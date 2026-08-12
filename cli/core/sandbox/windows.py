"""Windows sandbox backend built on ctypes (no compiled helpers).

Follows the Codex Windows sandbox design:

- dedicated local users ``CodewoodSandboxOffline`` / ``CodewoodSandboxOnline``
  provide the file-system identity (reads follow what those users may read,
  writes only where the workspace ACL grants them);
- a Windows Firewall outbound BLOCK rule keyed on the offline user provides
  network isolation without runtime elevation;
- ``CreateProcessWithLogonW`` starts commands as the sandbox user (standard
  interactive users hold ``SeImpersonatePrivilege``);
- a Job Object with ``KILL_ON_JOB_CLOSE`` manages the process-tree lifecycle;
- the sandbox passwords are stored DPAPI-encrypted next to the config.

Provisioning (user creation + firewall rules) requires elevation; the ACL
steps run on the user's own files and need none.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import random
import secrets
import shutil
import string
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .base import SandboxBackend
from . import SANDBOX_USER_OFFLINE, SANDBOX_USER_ONLINE

# Hard-coded app logger prefix: this module may be loaded by absolute path
# (codewood_sandbox_runtime.windows), where relative imports do not resolve
# to the real "codewood" logger tree and its logs would never reach the file.
_log = logging.getLogger("codewood.sandbox.windows")

SANDBOX_SECRET_FILENAME = "sandbox_secret.bin"
SANDBOX_PROVISIONED_FLAG = "sandbox_provisioned.flag"
SANDBOX_FIREWALL_RULE_OFFLINE = "Codewood Sandbox Offline Block Outbound"
SANDBOX_RUNTIME_DIRNAME = "sandbox"
SANDBOX_USERS_GROUP = "CodewoodSandUsers"
SANDBOX_CAP_SID_FILENAME = "sandbox_cap_sid.json"
SANDBOX_ACL_RECORD_FILENAME = "sandbox_acl_dirs.json"
# Random per-machine capability SIDs (Codex ``cap_sid`` design).  The restricted
# token's write access is granted only where these SIDs have explicit ACL
# entries, so group-inherited write permissions (e.g. "Authenticated Users:
# Modify") do not leak into the sandbox.  ``workspace`` gates workspace writes;
# ``readonly`` is used by read_only sessions and may write only the sandbox
# runtime dirs.
# ``CreateProcessWithLogonW`` rejects command lines longer than ~930
# characters with ERROR_INVALID_PARAMETER (0x80070057), far below the 32K
# limit of plain ``CreateProcess``.  The runner argv embeds the whole user
# command, so commands beyond this budget fail to start.  The safe budget for
# the complete runner command line (kept well under the observed ~930 limit).
_LOGONW_CMDLINE_SAFE_LIMIT = 800
HANDLE_FLAG_INHERIT = 0x1
WRITE_RESTRICTED = 0x8
LUA_TOKEN = 0x4

#: Short-lived cache of credential checks (config dir -> (timestamp, ok)).
#: The settings page verifies the sandbox users' passwords on load; without a
#: cache, repeated page loads with a mismatched secret would accumulate failed
#: logon attempts and could trip the account lockout threshold.
_credential_check_cache: Dict[str, Tuple[float, bool]] = {}
_CREDENTIAL_CHECK_TTL = 60.0

# Win32 constants -----------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
SW_SHOWNORMAL = 1
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
CRYPTPROTECT_UI_FORBIDDEN = 0x1
LOGON32_LOGON_INTERACTIVE = 2
LOGON32_PROVIDER_DEFAULT = 0
DISABLE_MAX_PRIVILEGES = 0x1


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (n, ctypes.c_ulonglong)
        for n in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


_WIN: Optional[Dict[str, Any]] = None


def _win() -> Dict[str, Any]:
    """Lazily load and configure the Win32 entry points used by the backend."""
    global _WIN
    if _WIN is not None:
        return _WIN
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    create_process = advapi32.CreateProcessWithLogonW
    create_process.restype = wintypes.BOOL
    create_process.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        ctypes.POINTER(PROCESS_INFORMATION),
    ]

    logon_user = advapi32.LogonUserW
    logon_user.restype = wintypes.BOOL
    logon_user.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]

    create_restricted = advapi32.CreateRestrictedToken
    create_restricted.restype = wintypes.BOOL
    create_restricted.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.HANDLE),
    ]

    create_with_token = advapi32.CreateProcessWithTokenW
    create_with_token.restype = wintypes.BOOL
    create_with_token.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]

    convert_sid = advapi32.ConvertStringSidToSidW
    convert_sid.restype = wintypes.BOOL
    convert_sid.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    free_sid = advapi32.FreeSid
    free_sid.restype = ctypes.c_void_p
    free_sid.argtypes = [ctypes.c_void_p]

    create_job = kernel32.CreateJobObjectW
    create_job.restype = wintypes.HANDLE
    create_job.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]

    set_job_info = kernel32.SetInformationJobObject
    set_job_info.restype = wintypes.BOOL
    set_job_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]

    assign_job = kernel32.AssignProcessToJobObject
    assign_job.restype = wintypes.BOOL
    assign_job.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    close_handle = kernel32.CloseHandle
    close_handle.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]

    set_handle_info = kernel32.SetHandleInformation
    set_handle_info.restype = wintypes.BOOL
    set_handle_info.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]

    wait_single = kernel32.WaitForSingleObject
    wait_single.restype = wintypes.DWORD
    wait_single.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    terminate = kernel32.TerminateProcess
    terminate.restype = wintypes.BOOL
    terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]

    get_exit = kernel32.GetExitCodeProcess
    get_exit.restype = wintypes.BOOL
    get_exit.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    local_free = kernel32.LocalFree
    local_free.restype = wintypes.HLOCAL
    local_free.argtypes = [wintypes.HLOCAL]

    lookup_account = advapi32.LookupAccountNameW
    lookup_account.restype = wintypes.BOOL
    lookup_account.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_int),
    ]

    protect = crypt32.CryptProtectData
    protect.restype = wintypes.BOOL
    protect.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]

    unprotect = crypt32.CryptUnprotectData
    unprotect.restype = wintypes.BOOL
    unprotect.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]

    shell_exec = shell32.ShellExecuteW
    shell_exec.restype = wintypes.HINSTANCE
    shell_exec.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]

    open_process = kernel32.OpenProcess
    open_process.restype = wintypes.HANDLE
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    open_process_token = advapi32.OpenProcessToken
    open_process_token.restype = wintypes.BOOL
    open_process_token.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]

    get_token_info = advapi32.GetTokenInformation
    get_token_info.restype = wintypes.BOOL
    get_token_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    lookup_sid = advapi32.LookupAccountSidW
    lookup_sid.restype = wintypes.BOOL
    lookup_sid.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_int),
    ]

    # ConPTY is available from Windows 10 version 1809 onward.  Resolve these
    # APIs dynamically so older Windows versions retain the pipe-based,
    # sandboxed fallback instead of failing to load the backend.
    create_pseudo_console = getattr(kernel32, "CreatePseudoConsole", None)
    close_pseudo_console = getattr(kernel32, "ClosePseudoConsole", None)
    init_attr_list = getattr(kernel32, "InitializeProcThreadAttributeList", None)
    update_attr = getattr(kernel32, "UpdateProcThreadAttribute", None)
    if all((create_pseudo_console, close_pseudo_console, init_attr_list, update_attr)):
        create_pseudo_console.restype = ctypes.c_long  # HRESULT
        create_pseudo_console.argtypes = [
            COORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        close_pseudo_console.restype = None
        close_pseudo_console.argtypes = [ctypes.c_void_p]
        init_attr_list.restype = wintypes.BOOL
        init_attr_list.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        update_attr.restype = wintypes.BOOL
        update_attr.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]

    _WIN = {
        "CreateProcessWithLogonW": create_process,
        "LogonUserW": logon_user,
        "CreateRestrictedToken": create_restricted,
        "CreateProcessWithTokenW": create_with_token,
        "ConvertStringSidToSidW": convert_sid,
        "FreeSid": free_sid,
        "OpenProcess": open_process,
        "OpenProcessToken": open_process_token,
        "GetTokenInformation": get_token_info,
        "SetHandleInformation": set_handle_info,
        "LookupAccountSidW": lookup_sid,
        "CreateJobObjectW": create_job,
        "SetInformationJobObject": set_job_info,
        "AssignProcessToJobObject": assign_job,
        "CloseHandle": close_handle,
        "WaitForSingleObject": wait_single,
        "TerminateProcess": terminate,
        "GetExitCodeProcess": get_exit,
        "LocalFree": local_free,
        "LookupAccountNameW": lookup_account,
        "CryptProtectData": protect,
        "CryptUnprotectData": unprotect,
        "ShellExecuteW": shell_exec,
        "CreatePseudoConsole": create_pseudo_console,
        "ClosePseudoConsole": close_pseudo_console,
        "InitializeProcThreadAttributeList": init_attr_list,
        "UpdateProcThreadAttribute": update_attr,
    }
    return _WIN


def _random_password(length: int = 14) -> str:
    # Keep it <= 14 characters: net.exe prompts for Y/N confirmation on longer
    # passwords and would hang on a pipe without stdin.
    #
    # Windows password complexity (when enabled) requires at least 3 of 4
    # character classes; a pure random draw can by chance miss a class
    # entirely (e.g. no digits) and New-LocalUser then rejects the password
    # with InvalidPasswordException.  Force one character from each of
    # upper/lower/digit/symbol so the result satisfies any complexity policy,
    # then fill the rest randomly.  The symbol set avoids quotes/backslashes
    # so the password stays safe inside PowerShell single-quoted strings.
    symbols = "!@#%^&*()-_=+"
    alphabet = string.ascii_letters + string.digits + symbols
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(symbols),
    ]
    chars.extend(secrets.choice(alphabet) for _ in range(max(0, length - 4)))
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


def _verify_local_user_password(user: str, password: str) -> bool:
    """Check a local account password with ``LogonUserW`` (no session created).

    Uses the same interactive logon and local-domain (``.``) semantics as
    ``CreateProcessWithLogonW`` in :meth:`WindowsSandboxBackend.spawn`, so a
    ``True`` result means sandboxed commands can actually start under the
    stored secret.
    """
    try:
        w = _win()
        h_token = wintypes.HANDLE()
        ok = w["LogonUserW"](
            user,
            ".",
            password,
            LOGON32_LOGON_INTERACTIVE,
            LOGON32_PROVIDER_DEFAULT,
            ctypes.byref(h_token),
        )
        if ok:
            try:
                w["CloseHandle"](h_token)
            except Exception:
                pass
            return True
        return False
    except Exception:
        return False


def _verify_credentials_cached(
    config_dir: Any, secret: Dict[str, str], fresh: bool = False
) -> bool:
    key = str(Path(config_dir).resolve())
    now = time.time()
    if not fresh:
        cached = _credential_check_cache.get(key)
        if cached is not None and now - cached[0] < _CREDENTIAL_CHECK_TTL:
            return cached[1]
    ok = _verify_local_user_password(
        SANDBOX_USER_OFFLINE, secret.get("offline", "")
    ) and _verify_local_user_password(SANDBOX_USER_ONLINE, secret.get("online", ""))
    _credential_check_cache[key] = (now, ok)
    return ok


def _run_process(argv: list, timeout: float = 180, stdin_data: Optional[str] = None) -> Any:
    """Run a system helper (net/icacls/netsh/powershell) and capture output."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            input=stdin_data,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # pragma: no cover - defensive
        return SimpleNamespace(returncode=-1, stdout="", stderr=str(exc))


# Complete write-side deny set for read_only workspaces / protected dirs.
# Deliberately excludes SYNCHRONIZE (part of icacls's W) and keeps cmd.exe's
# built-in dir/type readable while blocking create/modify/append/delete.
_DENY_WRITE_RIGHTS = (
    "CreateFiles,AppendData,WriteData,DeleteSubdirectoriesAndFiles,Delete,"
    "WriteAttributes,WriteExtendedAttributes,ChangePermissions,TakeOwnership"
)

def _ps_grant_modify_sid(path: str, sid: str) -> None:
    """Grant the capability SID Modify (container+object inherit) on ``path``."""
    ps = (
        "$p='{0}'; $acl=Get-Acl -LiteralPath $p; "
        "$sid=New-Object System.Security.Principal.SecurityIdentifier('{2}'); "
        "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule("
        "$sid,'Modify','ContainerInherit,ObjectInherit','None','Allow'); "
        "$acl.AddAccessRule($rule); Set-Acl -LiteralPath $p $acl"
    ).format(path, "", sid)
    _run_process(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])


def _ps_grant_read_group(path: str) -> int:
    """Grant the sandbox users group ReadAndExecute on ``path`` (no elevation).

    Used by provisioning for the Python interpreter that launches the runner
    in source builds (packaged builds run ``shell-runner.exe`` instead). The
    runner process itself uses the plain logon token, so a group ACE is
    sufficient -- the restricted-token rule only applies to the child command.
    """
    ps = (
        "$p='{0}'; $acl=Get-Acl -LiteralPath $p; "
        "$id=New-Object System.Security.Principal.NTAccount('{1}'); "
        "$sid=$id.Translate([System.Security.Principal.SecurityIdentifier]); "
        "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule("
        "$sid,'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'); "
        "$acl.AddAccessRule($rule); Set-Acl -LiteralPath $p $acl"
    ).format(path, SANDBOX_USERS_GROUP)
    return _run_process(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]).returncode


#: One PowerShell process applies every workspace ACL edit (root + protected
#: subdirectories) instead of the previous per-ACE helpers that each spawned a
#: fresh ``powershell.exe`` (~0.5-1.5s startup each, ~25 processes per call).
#: It also skips ``Set-Acl`` when the managed ACE set is already correct, so
#: repeated refreshes do not re-propagate inheritance over the whole tree.
_WORKSPACE_ACL_PS = r"""
$ErrorActionPreference = 'Stop'
$group = '@@GROUP@@'
$users = @(@@USERS@@)
$capW = '@@CAPW@@'
$capR = '@@CAPR@@'
$deny = '@@DENY@@'

$managedSids = @()
foreach ($u in $users) {
    try { $managedSids += (New-Object System.Security.Principal.NTAccount($u)).Translate([System.Security.Principal.SecurityIdentifier]).Value } catch {}
}
try { $managedSids += (New-Object System.Security.Principal.NTAccount($group)).Translate([System.Security.Principal.SecurityIdentifier]).Value } catch {}
if ($capW) { $managedSids += $capW }
if ($capR) { $managedSids += $capR }

function Get-SidValue($ace) {
    try { return $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { return $null }
}

function New-Rule([object]$identity, [string]$rights, [string]$type) {
    return New-Object System.Security.AccessControl.FileSystemAccessRule($identity, $rights, 'ContainerInherit,ObjectInherit', 'None', $type)
}

function Rule-Equals($ace, $rule) {
    if ($ace.AccessControlType -ne $rule.AccessControlType) { return $false }
    if ($ace.FileSystemRights -ne $rule.FileSystemRights) { return $false }
    if ($ace.InheritanceFlags -ne $rule.InheritanceFlags) { return $false }
    if ($ace.PropagationFlags -ne $rule.PropagationFlags) { return $false }
    $a = Get-SidValue $ace
    $b = Get-SidValue $rule
    if (-not $a -or -not $b) { return $false }
    return ($a -eq $b)
}

function Apply-ACL([string]$path, [string]$mode) {
    $acl = Get-Acl -LiteralPath $path
    $managed = @()
    foreach ($ace in $acl.Access) {
        $sid = Get-SidValue $ace
        if ($sid -and ($managedSids -contains $sid)) { $managed += $ace }
    }
    $desired = @()
    $groupId = New-Object System.Security.Principal.NTAccount($group)
    if ($capW) { $capWId = New-Object System.Security.Principal.SecurityIdentifier($capW) } else { $capWId = $null }
    if ($capR) { $capRId = New-Object System.Security.Principal.SecurityIdentifier($capR) } else { $capRId = $null }
    if ($mode -eq 'readonly') {
        $desired += New-Rule $groupId 'ReadAndExecute' 'Allow'
        $desired += New-Rule $groupId $deny 'Deny'
        if ($capWId) { $desired += New-Rule $capWId $deny 'Deny' }
        if ($capRId) { $desired += New-Rule $capRId $deny 'Deny' }
    } elseif ($mode -eq 'write') {
        $desired += New-Rule $groupId 'Modify' 'Allow'
        if ($capWId) { $desired += New-Rule $capWId 'Modify' 'Allow' }
    } else {
        $desired += New-Rule $groupId $deny 'Deny'
        if ($capWId) { $desired += New-Rule $capWId $deny 'Deny' }
        if ($capRId) { $desired += New-Rule $capRId $deny 'Deny' }
    }
    $allPresent = $true
    foreach ($rule in $desired) {
        $found = $false
        foreach ($ace in $managed) {
            if (Rule-Equals $ace $rule) { $found = $true; break }
        }
        if (-not $found) { $allPresent = $false; break }
    }
    if ($allPresent -and ($managed.Count -eq $desired.Count)) { return }
    foreach ($ace in $managed) { $acl.RemoveAccessRule($ace) | Out-Null }
    foreach ($rule in $desired) { $acl.AddAccessRule($rule) }
    try {
        Set-Acl -LiteralPath $path -AclObject $acl
    } catch {
        [Console]::Error.WriteLine(("ACL apply failed on {0}: {1}" -f $path, $_))
    }
}

try { Apply-ACL '@@ROOT@@' '@@ROOTMODE@@' } catch { [Console]::Error.WriteLine(("ACL apply failed on root: {0}" -f $_)) }
@@PROTECTED_CALLS@@
"""


def _build_workspace_acl_script(
    ws: str,
    root_mode: str,
    group: str,
    users: Sequence[str],
    cap_sids: Optional[Dict[str, str]],
    protected: Sequence[str],
) -> str:
    """Build the single PowerShell script for :meth:`WindowsSandboxBackend.apply_workspace_acls`."""

    def q(value: str) -> str:
        return value.replace("'", "''")

    protected_calls = ""
    if protected:
        lines = []
        for p in protected:
            lines.append(
                "try { Apply-ACL '%s' 'protect' } catch { "
                "[Console]::Error.WriteLine(('ACL apply failed on protected dir: ' + $_)) }"
                % q(p)
            )
        protected_calls = "\n".join(lines) + "\n"
    return (
        _WORKSPACE_ACL_PS.replace("@@GROUP@@", group)
        .replace("@@USERS@@", ",".join("'" + q(u) + "'" for u in users))
        .replace("@@CAPW@@", cap_sids["workspace"] if cap_sids else "")
        .replace("@@CAPR@@", cap_sids["readonly"] if cap_sids else "")
        .replace("@@DENY@@", _DENY_WRITE_RIGHTS)
        .replace("@@ROOT@@", q(ws))
        .replace("@@ROOTMODE@@", root_mode)
        .replace("@@PROTECTED_CALLS@@", protected_calls)
    )


#: Removes capability-SID ACEs and dead (no longer resolvable) local SIDs from
#: a workspace root.  icacls cannot match synthetic SIDs or dead SIDs (it
#: resolves names), so this is done with .NET ACL APIs; removing the root's
#: inheritable ACEs propagates to children through auto-inheritance.
_CLEANUP_ROOT_ACL_PS = r"""
$p = '@@ROOT@@'
$capW = '@@CAPW@@'
$capR = '@@CAPR@@'
$acl = Get-Acl -LiteralPath $p
$drop = New-Object System.Collections.Generic.List[object]
foreach ($ace in $acl.Access) {
    $sid = $null
    try { $sid = $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { continue }
    if ($capW -and $sid -eq $capW) { $drop.Add($ace); continue }
    if ($capR -and $sid -eq $capR) { $drop.Add($ace); continue }
    if ($sid -like 'S-1-5-21-*') {
        $resolves = $true
        try {
            [void](New-Object System.Security.Principal.NTAccount($sid)).Translate([System.Security.Principal.SecurityIdentifier])
        } catch { $resolves = $false }
        if (-not $resolves) { $drop.Add($ace) }
    }
}
foreach ($ace in $drop) { $acl.RemoveAccessRule($ace) | Out-Null }
if ($drop.Count -gt 0) { Set-Acl -LiteralPath $p -AclObject $acl }
"""


def _build_cleanup_root_script(ws: str, cap_w: str, cap_r: str) -> str:
    """Build the root-level dead/capability-SID cleanup script."""
    return (
        _CLEANUP_ROOT_ACL_PS.replace("@@ROOT@@", ws.replace("'", "''"))
        .replace("@@CAPW@@", cap_w)
        .replace("@@CAPR@@", cap_r)
    )


def _cap_sid_path(config_dir: Any) -> Path:
    return _shared_sandbox_root() / SANDBOX_CAP_SID_FILENAME


_SANDBOX_ROOT_OVERRIDE = os.environ.get("CODEWOOD_SANDBOX_ROOT", "").strip()


def _shared_sandbox_root() -> Path:
    """Shared, machine-local sandbox state root (independent of the config dir).

    Multiple Code Wood data directories on the same machine share one sandbox
    installation: users, passwords, capability SIDs, the provisioning flag,
    the runtime dirs and the ACL record all live under
    ``%LOCALAPPDATA%`` + app slug + ``sandbox`` instead of inside each config
    directory, so recreating users in one instance never orphans the others.
    Overridable with ``CODEWOOD_SANDBOX_ROOT`` (used by tests).
    """
    if _SANDBOX_ROOT_OVERRIDE:
        return Path(_SANDBOX_ROOT_OVERRIDE).resolve()
    base = Path(
        os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    )
    try:
        from ..config.app_info import get_app_slug_compact

        slug = get_app_slug_compact()
    except Exception:  # pragma: no cover - absolute-path loading fallback
        slug = "codewood"
    return (base / slug / "sandbox").resolve()


def _acl_record_path() -> Path:
    return _shared_sandbox_root() / SANDBOX_ACL_RECORD_FILENAME


def _load_acl_record() -> set:
    """Return the set of directories whose ACLs the sandbox manages."""
    try:
        data = json.loads(_acl_record_path().read_text(encoding="utf-8"))
        dirs = data.get("dirs") or []
        try:
            return {str(Path(d).resolve()) for d in dirs if d}
        except Exception:
            return {str(d) for d in dirs if d}
    except Exception:
        return set()


def _save_acl_record(dirs: set) -> None:
    path = _acl_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"dirs": sorted(dirs)}, ensure_ascii=False), encoding="utf-8"
    )
    try:
        tmp.replace(path)
    except OSError:  # pragma: no cover - defensive
        tmp.write_text(json.dumps({"dirs": sorted(dirs)}), encoding="utf-8")


def _record_acl_dirs(paths) -> None:
    """Add directories to the ACL record (best effort)."""
    if not paths:
        return
    try:
        rec = _load_acl_record()
        rec.update(
            str(Path(p).resolve()) for p in paths if p
        )
        _save_acl_record(rec)
    except Exception:
        pass


def _unrecord_acl_dirs(paths) -> None:
    """Drop directories from the ACL record (best effort)."""
    if not paths:
        return
    try:
        rec = _load_acl_record()
        rec.difference_update(
            str(Path(p).resolve()) for p in paths if p
        )
        _save_acl_record(rec)
    except Exception:
        pass


def _make_cap_sid() -> str:
    """Random synthetic SID (Codex uses S-1-5-21 with random RIDs)."""
    return "S-1-5-21-{}-{}-{}-{}".format(
        secrets.randbits(32), secrets.randbits(32),
        secrets.randbits(32), secrets.randbits(32),
    )


def _load_or_create_cap_sids(config_dir: Any) -> Dict[str, str]:
    """Load (or create+persist) the per-machine capability SIDs."""
    path = _cap_sid_path(config_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("workspace") and data.get("readonly"):
                return data
        except Exception:
            pass
    caps = {"workspace": _make_cap_sid(), "readonly": _make_cap_sid()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(caps), encoding="utf-8")
    return caps


def _cap_sid_for_level(config_dir: Any, level: str) -> str:
    caps = _load_or_create_cap_sids(config_dir)
    return caps["readonly"] if level == "read_only" else caps["workspace"]


def _shell_runner_exe_path() -> Optional[str]:
    """Locate the packaged ``shell-runner.exe`` next to the frozen app.

    The onedir bundle ships ``shell-runner.exe`` in the same folder as
    ``codewood.exe`` (built together by ``build/codewood.spec``) so both share
    one ``_internal`` runtime. The older nested layout is accepted as a
    fallback. Returns ``None`` in development so the python ``-c`` loader
    path is used instead.
    """
    if not getattr(sys, "frozen", False):
        return None
    base = Path(sys.executable).resolve().parent
    for candidate in (
        base / "shell-runner.exe",
        base / "shell-runner" / "shell-runner.exe",
        base / "_internal" / "shell-runner" / "shell-runner.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _ensure_sandbox_runner_copy(runner_exe: str) -> str:
    """Return a runner path the sandbox users can read and execute.

    ``CreateProcessWithLogonW`` resolves the executable image (and the
    working directory) *in the security context of the sandbox user*.  A
    packaged app whose ``shell-runner.exe`` lives in a location the sandbox
    users cannot traverse (a per-user install under the installing user's
    profile, a locked-down install directory, or a mapped network drive
    letter) therefore fails at process creation with ``ERROR_ACCESS_DENIED``
    (5), even though the app itself reads the file without trouble.  Source
    runs never hit this because the runner is the venv interpreter inside an
    ACL-open tree.

    The sandbox runtime dirs under ``tmp`` are ACL-granted to the sandbox
    users during provisioning, so mirror the runner bundle there and launch
    from the mirror.  The mirror is refreshed when the source executable is
    newer (an app update).  Best-effort: any failure returns the original
    path so the regular (now diagnostic) spawn error is raised instead of
    silently changing behaviour.
    """
    try:
        src_exe = Path(runner_exe).resolve()
        if not src_exe.is_file():
            return runner_exe
        src_dir = src_exe.parent
        app_base = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else None
        )
        target_root = _shared_sandbox_root() / "tmp" / "runner"
        internal_name = "_internal"

        def _fresh(dst_exe: Path, src_internal: Path, dst_internal: Path) -> bool:
            return bool(
                dst_exe.is_file()
                and os.path.getmtime(src_exe) <= os.path.getmtime(dst_exe)
                and (not src_internal.is_dir() or dst_internal.is_dir())
            )

        if app_base is not None and src_dir == app_base:
            # Flat layout (shell-runner.exe next to codewood.exe): mirror only
            # the executable plus its PyInstaller ``_internal`` sibling, never
            # the whole application bundle.
            dst_dir = target_root / "_flat"
            dst_exe = dst_dir / src_exe.name
            src_internal = app_base / internal_name
            dst_internal = dst_dir / internal_name
            if not _fresh(dst_exe, src_internal, dst_internal):
                if dst_dir.exists():
                    shutil.rmtree(dst_dir, ignore_errors=True)
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_exe, dst_exe)
                if src_internal.is_dir():
                    shutil.copytree(src_internal, dst_internal, symlinks=False)
            mirrored = str(dst_exe)
        else:
            # Nested bundle layout (dist\\codewood\\shell-runner\\shell-runner.exe
            # with its own _internal): mirror the whole small bundle directory.
            dst_dir = target_root / src_dir.name
            dst_exe = dst_dir / src_exe.name
            if not _fresh(dst_exe, src_dir / internal_name, dst_dir / internal_name):
                if dst_dir.exists():
                    shutil.rmtree(dst_dir, ignore_errors=True)
                shutil.copytree(
                    src_dir,
                    dst_dir,
                    symlinks=False,
                    ignore=shutil.ignore_patterns("*.pkg"),
                )
            mirrored = str(dst_exe)
        _log.info(
            "sandbox runner mirrored: %s -> %s", runner_exe, mirrored
        )
        return mirrored
    except Exception:
        _log.info(
            "sandbox runner mirror failed for %r; using original", runner_exe,
            exc_info=True,
        )
        return runner_exe


def _spawn_access_denied_hint(runner: str, cwd: str) -> str:
    """Compose an actionable message for ERROR_ACCESS_DENIED from the spawn."""
    parts = []
    try:
        import ctypes as _ct

        get_drive_type = _ct.windll.kernel32.GetDriveTypeW
        get_drive_type.restype = _ct.c_uint
        get_drive_type.argtypes = [_ct.c_wchar_p]
        DRIVE_REMOTE = 4
        for label, path in (("runner", runner), ("working directory", cwd)):
            try:
                drive = _ct.create_unicode_buffer(path[:3])
                if get_drive_type(drive) == DRIVE_REMOTE:
                    parts.append(
                        f"the {label} is on a network drive letter ({path[:3]}); "
                        "CreateProcessWithLogonW cannot use drive letters for the "
                        "sandbox user"
                    )
            except Exception:
                pass
    except Exception:
        pass
    parts.append(
        "the sandbox user could not access the runner executable or the "
        f"working directory (paths: runner={runner!r}, cwd={cwd!r})"
    )
    parts.append(
        "for a per-user install the sandbox users cannot traverse the "
        "installing user's profile: reinstall for all users, or grant "
        "read+execute on the install directory to the CodewoodSandUsers group"
    )
    return " ".join(parts)


def _user_exists(name: str) -> bool:
    """Check whether a local account exists (no admin required)."""
    try:
        w = _win()
        sid_size = wintypes.DWORD(0)
        dom_size = wintypes.DWORD(0)
        use = ctypes.c_int(0)
        ok = w["LookupAccountNameW"](
            None,
            name,
            None,
            ctypes.byref(sid_size),
            None,
            ctypes.byref(dom_size),
            ctypes.byref(use),
        )
        if ok:
            return True
        err = ctypes.get_last_error()
        # ERROR_NONE_MAPPED / ERROR_INVALID_ACCOUNT_NAME => does not exist.
        return err not in (1332, 1231)
    except Exception:
        return False


def _process_user_sid(pid: int) -> str:
    """Return the token user name of a process (diagnostics)."""
    try:
        w = _win()
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = w["OpenProcess"](PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h_proc:
            return "?"
        try:
            h_token = wintypes.HANDLE()
            if not w["OpenProcessToken"](h_proc, 0x0008, ctypes.byref(h_token)):
                return "?"
            try:
                buf = ctypes.create_string_buffer(4096)
                size = wintypes.DWORD()
                if not w["GetTokenInformation"](
                    h_token, 1, buf, len(buf), ctypes.byref(size)
                ):
                    return "?"
                # TOKEN_USER: { SID *User; } — SID pointer is the first field.
                sid = ctypes.cast(
                    ctypes.cast(buf, ctypes.POINTER(ctypes.c_size_t))[0],
                    ctypes.c_void_p,
                )
                name = ctypes.create_unicode_buffer(256)
                dom = ctypes.create_unicode_buffer(256)
                n1 = wintypes.DWORD(256)
                n2 = wintypes.DWORD(256)
                use = ctypes.c_int(0)
                if w["LookupAccountSidW"](
                    None, sid, name, ctypes.byref(n1), dom, ctypes.byref(n2), ctypes.byref(use)
                ):
                    return f"{dom.value}\\{name.value}"
                return "sid?"
            finally:
                w["CloseHandle"](h_token)
        finally:
            w["CloseHandle"](h_proc)
    except Exception:
        return "?"


def _secret_path(config_dir: Any) -> Path:
    return _shared_sandbox_root() / SANDBOX_SECRET_FILENAME


def _flag_path(config_dir: Any) -> Path:
    return _shared_sandbox_root() / SANDBOX_PROVISIONED_FLAG


def _dpapi_protect(data: bytes) -> bytes:
    w = _win()
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    ok = w["CryptProtectData"](
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.WinError(ctypes.get_last_error()))
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        w["LocalFree"](blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    w = _win()
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    ok = w["CryptUnprotectData"](
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.WinError(ctypes.get_last_error()))
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        w["LocalFree"](blob_out.pbData)


def _load_secret(config_dir: Any) -> Optional[Dict[str, str]]:
    path = _secret_path(config_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(_dpapi_unprotect(path.read_bytes()).decode("utf-8"))
        if isinstance(data, dict) and data.get("offline") and data.get("online"):
            return data
    except Exception:
        pass
    return None


def _save_secret(config_dir: Any, data: Dict[str, str]) -> None:
    path = _secret_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi_protect(json.dumps(data).encode("utf-8")))


def _ensure_secret(config_dir: Any) -> Dict[str, str]:
    data = _load_secret(config_dir)
    if data is not None:
        return data
    data = {"offline": _random_password(), "online": _random_password()}
    _save_secret(config_dir, data)
    return data


def _fd_handle(fd: int) -> int:
    import msvcrt

    return int(msvcrt.get_osfhandle(fd))


def _build_env_block(env: dict) -> Any:
    items = []
    for key, value in env.items():
        ks = str(key)
        vs = str(value)
        if "=" in ks or "\x00" in ks or "\x00" in vs:
            continue
        items.append(f"{ks}={vs}")
    # CreateProcessWithLogonW rejects environment entries whose names start
    # with '=' (drive-letter current-directory variables); they are skipped
    # above. The block must end with a double null terminator.
    block = "\x00".join(items) + "\x00\x00"
    # A c_wchar array (unlike create_unicode_buffer) preserves embedded NULs,
    # which the environment block relies on.
    return (ctypes.c_wchar * len(block))(*block)


class WindowsSandboxProcess:
    """Duck-typed ``subprocess.Popen`` backed by a sandboxed Win32 process."""

    def __init__(
        self,
        w: Dict[str, Any],
        h_process: int,
        h_thread: int,
        h_job: int,
        pid: int,
        stdout: Any,
        stdin: Any,
        exit_file: Optional[str] = None,
        cleanup_paths: Optional[Sequence[str]] = None,
        pseudo_console: Optional[ctypes.c_void_p] = None,
    ) -> None:
        self._w = w
        self._hproc = h_process
        self._hthread = h_thread
        self._hjob = h_job
        self.pid = pid
        self.stdout = stdout
        self.stdin = stdin
        self._exit_file = exit_file
        self._cleanup_paths = list(cleanup_paths or [])
        self._returncode: Optional[int] = None
        self._job_closed = False
        self._pseudo_console = pseudo_console
        self._pseudo_console_closed = False

    def poll(self) -> Optional[int]:
        if self._returncode is not None:
            return self._returncode
        self._read_exit_if_finished()
        return self._returncode

    def _read_exit_if_finished(self) -> bool:
        """If the runner exited, read the child exit code from the exit pipe."""
        if self._returncode is not None:
            return True
        rc = self._w["WaitForSingleObject"](self._hproc, 0)
        if rc != WAIT_OBJECT_0:
            return False
        self._returncode = self._consume_exit_code()
        self._close_pseudo_console()
        return True

    def _consume_exit_code(self) -> int:
        try:
            if self._exit_file:
                with open(self._exit_file, "rb") as f:
                    data = f.read(4)
                if len(data) == 4:
                    value = struct.unpack("<I", data)[0]
                    return value if value != 0xFFFFFFFF else -1
        except Exception:
            pass
        finally:
            # The exit file is a one-shot channel; drop it once consumed so
            # the sandbox runtime tmp dir does not accumulate per-command files.
            if self._exit_file:
                try:
                    os.unlink(self._exit_file)
                except OSError:
                    pass
            for extra in self._cleanup_paths:
                try:
                    os.unlink(extra)
                except OSError:
                    pass
        return -1

    @property
    def returncode(self) -> Optional[int]:
        return self.poll()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if self._returncode is not None:
            return self._returncode
        ms = int(timeout * 1000) if timeout is not None else INFINITE
        if ms < 0:
            ms = 0
        rc = self._w["WaitForSingleObject"](self._hproc, ms)
        if rc == WAIT_TIMEOUT:
            return None
        self._returncode = self._consume_exit_code()
        self._close_pseudo_console()
        return self._returncode

    def kill(self) -> None:
        # Terminate the root process, then close the job handle: the
        # KILL_ON_JOB_CLOSE limit takes down the whole process tree, which
        # a cross-user ``taskkill`` cannot do without elevation.
        try:
            self._w["TerminateProcess"](self._hproc, 1)
        except Exception:
            pass
        self._close_job()
        self._close_pseudo_console()

    def _close_pseudo_console(self) -> None:
        if self._pseudo_console_closed:
            return
        self._pseudo_console_closed = True
        try:
            if self._pseudo_console and self._w.get("ClosePseudoConsole"):
                self._w["ClosePseudoConsole"](self._pseudo_console)
        except Exception:
            pass

    def _close_job(self) -> None:
        if self._job_closed:
            return
        self._job_closed = True
        try:
            if self._hjob:
                self._w["CloseHandle"](self._hjob)
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            if not self._job_closed:
                if self.poll() is None:
                    try:
                        self._w["TerminateProcess"](self._hproc, 1)
                    except Exception:
                        pass
                self._close_job()
                self._close_pseudo_console()
        except Exception:
            pass
        for handle in (self._hthread, self._hproc):
            try:
                if handle:
                    self._w["CloseHandle"](handle)
            except Exception:
                pass


def _select_user(level: str, network: bool) -> str:
    if level == "read_only":
        return SANDBOX_USER_OFFLINE
    if level == "workspace_write":
        return SANDBOX_USER_ONLINE if network else SANDBOX_USER_OFFLINE
    raise ValueError(f"invalid sandbox level: {level!r}")


class WindowsSandboxBackend(SandboxBackend):
    name = "windows"

    def is_supported(self) -> bool:
        return sys.platform == "win32"

    def is_provisioned(
        self, config_dir: Any, workspace_root: Optional[str] = None
    ) -> bool:
        return bool(self.status(config_dir, workspace_root).get("provisioned"))

    def status(
        self, config_dir: Any, workspace_root: Optional[str] = None
    ) -> Dict[str, Any]:
        users_exist = _user_exists(SANDBOX_USER_OFFLINE) and _user_exists(
            SANDBOX_USER_ONLINE
        )
        group_exists = _user_exists(SANDBOX_USERS_GROUP)
        secret_exists = _secret_path(config_dir).exists()
        users_foreign = bool(users_exist and not secret_exists)
        cap_ok = _cap_sid_path(config_dir).exists()
        flag_ok = False
        firewall_ok = False
        flag = _flag_path(config_dir)
        if flag.exists():
            try:
                data = json.loads(flag.read_text(encoding="utf-8"))
                flag_ok = bool(data.get("provisioned"))
                firewall_ok = bool(data.get("firewall_ok"))
            except Exception:
                pass
        # A failed firewall step means network isolation is missing, so the
        # sandbox counts as not provisioned and the "Set up sandbox" action
        # stays visible for retry.
        provisioned = bool(
            users_exist
            and group_exists
            and secret_exists
            and cap_ok
            and flag_ok
            and firewall_ok
        )
        degraded = bool(flag_ok and not firewall_ok)
        return {
            "supported": True,
            "provisioned": provisioned,
            "name": self.name,
            "users_exist": users_exist,
            "group_exists": group_exists,
            "users_group": SANDBOX_USERS_GROUP,
            "secret_exists": secret_exists,
            "users_foreign": users_foreign,
            "cap_sids_ok": cap_ok,
            "flag_ok": flag_ok,
            "firewall_ok": firewall_ok,
            "degraded": degraded,
            "offline_user": SANDBOX_USER_OFFLINE,
            "online_user": SANDBOX_USER_ONLINE,
            "message": (
                None
                if provisioned
                else (
                    "The sandbox users are ready but the outbound network "
                    "block rule could not be created. Run 'codewood sandbox "
                    "setup' in an elevated terminal (or use Set up sandbox) "
                    "again to retry it."
                    if degraded
                    else "Run 'codewood sandbox setup' in an elevated terminal, "
                    "or use Settings > Security > Set up sandbox."
                )
            ),
        }

    def verify_credentials(
        self, config_dir: Any, fresh: bool = False
    ) -> Optional[bool]:
        """Whether the stored secret can log on both sandbox users.

        ``None`` means the check is not applicable (no secret yet, or the
        users are missing — the settings page already offers setup then).
        The result is cached briefly so repeated page loads cannot pile up
        failed logon attempts against the account lockout threshold.  Pass
        ``fresh=True`` to bypass the cache (e.g. right after a completed
        setup, when the cached result is known to predate it).
        """
        secret = _load_secret(config_dir)
        if not secret:
            return None
        if not (
            _user_exists(SANDBOX_USER_OFFLINE)
            and _user_exists(SANDBOX_USER_ONLINE)
        ):
            return None
        return _verify_credentials_cached(config_dir, secret, fresh=fresh)

    def spawn(
        self,
        command: str,
        cwd: str,
        env: dict,
        stdin_data: Optional[bytes],
        level: str,
        network: bool,
        config_dir: Any = None,
    ) -> WindowsSandboxProcess:
        if not command.strip():
            raise ValueError("command cannot be empty")
        user = _select_user(level, network)
        secret = _load_secret(config_dir) if config_dir is not None else None
        if not secret:
            raise RuntimeError(
                "sandbox secret missing; run 'codewood sandbox setup' first"
            )
        password = secret.get("online" if user == SANDBOX_USER_ONLINE else "offline")
        if not password:
            raise RuntimeError("sandbox secret is incomplete; re-run sandbox setup")

        w = _win()

        # Emulate subprocess's shell=True so cmd builtins and redirection are
        # interpreted by the sandboxed command processor. Keep exactly one
        # wrapper: an additional inner ``cmd /c`` changes quoting semantics.
        # Do not wrap ``command`` in quotes: cmd only strips the outer quotes
        # when the *whole* command line starts with a quote (the exe token
        # prevents that), so ``cmd.exe /c "echo hi"`` would hand the trailing
        # quote to the command. subprocess.shell=True uses the same bare
        # ``comspec /c <command>`` form.
        comspec = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
        cmdline = "{} /c {}".format(comspec, command)

        # Sandbox user environment: point profile/temp at writable dirs owned
        # by the sandbox users instead of the real user's profile.
        sandbox_root = _shared_sandbox_root()
        home = sandbox_root / "home"
        tmp = sandbox_root / "tmp"
        env2 = dict(env)
        env2["USERNAME"] = user
        env2["USERDOMAIN"] = os.environ.get("COMPUTERNAME", ".")
        env2["USERPROFILE"] = str(home)
        env2["HOME"] = str(home)
        env2["HOMEDRIVE"] = str(home.drive or "C:")
        env2["HOMEPATH"] = str(home)[len(str(home.drive)) :] if home.drive else str(home)
        env2["TEMP"] = str(tmp)
        env2["TMP"] = str(tmp)
        env2["APPDATA"] = str(home / "AppData" / "Roaming")
        env2["LOCALAPPDATA"] = str(home / "AppData" / "Local")
        # git refuses repositories owned by another user ("dubious
        # ownership"); the sandbox user legitimately reads repos owned by
        # the real user, so allow all safe.directory entries.
        env2["GIT_CONFIG_COUNT"] = "1"
        env2["GIT_CONFIG_KEY_0"] = "safe.directory"
        env2["GIT_CONFIG_VALUE_0"] = "*"
        env_block = _build_env_block(env2)

        # Pipes for the runner: it runs the real command under the sandbox
        # user's restricted token and inherits these handles (the child's
        # stdin/stdout flow straight through the runner).  The runner creates
        # its own ConPTY bound to the inherited std handles, so the parent
        # only ever deals with plain pipes.
        stdout_r, stdout_w = os.pipe()
        os.set_inheritable(stdout_w, True)
        h_stdout = _fd_handle(stdout_w)
        stdin_file: Any = None
        if stdin_data is not None:
            stdin_r, stdin_w = os.pipe()
            os.set_inheritable(stdin_r, True)
            h_stdin = _fd_handle(stdin_r)
            stdin_file = os.fdopen(stdin_w, "wb", buffering=0)
        else:
            nul_fd = os.open(os.devnull, os.O_RDONLY)
            os.set_inheritable(nul_fd, True)
            h_stdin = _fd_handle(nul_fd)

        # Job object with KILL_ON_JOB_CLOSE: the runner assigns the real
        # command to it; closing the handle here kills the whole tree.
        h_job = w["CreateJobObjectW"](None, None)
        if h_job:
            try:
                info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                w["SetInformationJobObject"](
                    h_job,
                    JobObjectExtendedLimitInformation,
                    ctypes.byref(info),
                    ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION),
                )
                # Make the handle inheritable so the runner can assign the child.
                w["SetHandleInformation"](h_job, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
            except Exception:
                try:
                    w["CloseHandle"](h_job)
                except Exception:
                    pass
                h_job = 0
        # The command runs under the WRITE_RESTRICTED token that the runner
        # derives from its own (sandbox-user) primary token.  The parent cannot
        # create that token directly: CreateProcessAsUserW from a non-sandbox
        # process fails with ERROR_PRIVILEGE_NOT_HELD (1314), which is why the
        # runner must be a sandbox-user process.
        cap_sid = _cap_sid_for_level(config_dir, level)
        # The runner reports the child exit code through a small file inside the
        # sandbox runtime dir (inherited pipe handles do not survive LogonW).
        exit_file = (
            _shared_sandbox_root() / "tmp"
            / f"exit-{secrets.token_hex(8)}.tmp"
        )
        # The runner must be a python that the sandbox user can actually read.
        # ``sys.executable`` may live under the real user's profile (venvs),
        # which the sandbox user cannot traverse; provisioning grants read
        # access to the interpreter, and this override covers embedded/runtime
        # deployments.
        # Three runner launch modes:
        # 1. CODOWN_SANDBOX_RUNNER_EXE: explicit standalone shell-runner.exe
        #    (used by tests and by frozen builds if autodetection is off).
        # 2. Frozen (PyInstaller): the onedir bundle ships shell-runner.exe
        #    next to codewood.exe; no Python interpreter exists at runtime.
        # 3. Development: run the bundled interpreter with ``-c`` and load
        #    windows_runner.py by file (importing the ``cli`` package would
        #    execute third-party imports the bare interpreter may lack).
        runner_exe = os.environ.get("CODOWN_SANDBOX_RUNNER_EXE")
        if not runner_exe and getattr(sys, "frozen", False):
            runner_exe = _shell_runner_exe_path()
        if runner_exe:
            if not os.environ.get("CODOWN_SANDBOX_RUNNER_EXE"):
                # Mirror the packaged runner into the ACL-granted sandbox
                # runtime dir: CreateProcessWithLogonW resolves the image in
                # the sandbox user's security context, so a runner inside a
                # per-user install (or on a mapped drive) is denied with
                # ERROR_ACCESS_DENIED (5) even though the app reads it fine.
                runner_exe = _ensure_sandbox_runner_copy(runner_exe)
            runner_head = [runner_exe]
        else:
            runner_python = (
                os.environ.get("CODOWN_SANDBOX_RUNNER_PYTHON") or sys.executable
            )
            runner_path = Path(__file__).resolve().parent / "windows_runner.py"
            runner_loader = (
                "import importlib.util as _u,sys as _s;"
                "_p=r'{}';_sp=_u.spec_from_file_location('cwrunner',_p);"
                "_m=_u.module_from_spec(_sp);_sp.loader.exec_module(_m);"
                "_s.exit(_m.main())"
            ).format(str(runner_path))
            runner_head = [runner_python, "-c", runner_loader]

        def _runner_argv(cmd_arg: str) -> list:
            return runner_head + [
                "--cmd",
                cmd_arg,
                "--cap",
                cap_sid,
                "--exit-file",
                str(exit_file),
                "--job",
                str(int(h_job)) if h_job else "0",
            ]

        # CreateProcessWithLogonW rejects command lines longer than ~930
        # characters with ERROR_INVALID_PARAMETER (0x80070057), far below the
        # 32K limit of plain CreateProcess.  The runner argv embeds the whole
        # user command, so commands beyond the budget fail to start even though
        # they would run fine unsandboxed.  Route oversized commands through a
        # temp file the runner reads instead: the LogonW command line then
        # stays tiny (--cmd-file <path>) while the runner re-reads the full
        # command from the ACL-granted runtime dir.
        cmd_file: Optional[Path] = None
        runner_cmdline = subprocess.list2cmdline(_runner_argv(cmdline))
        if len(runner_cmdline) > _LOGONW_CMDLINE_SAFE_LIMIT:
            tmp.mkdir(parents=True, exist_ok=True)
            cmd_file = tmp / f"cmd-{secrets.token_hex(8)}.txt"
            try:
                cmd_file.write_text(cmdline, encoding="utf-8")
            except OSError:
                _log.warning(
                    "sandbox cmd-file write failed (%s); "
                    "falling back to inline cmd",
                    cmd_file,
                    exc_info=True,
                )
                cmd_file = None
            if cmd_file is not None:
                runner_cmdline = subprocess.list2cmdline(
                    runner_head
                    + [
                        "--cmd-file",
                        str(cmd_file),
                        "--cap",
                        cap_sid,
                        "--exit-file",
                        str(exit_file),
                        "--job",
                        str(int(h_job)) if h_job else "0",
                    ]
                )
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdInput = h_stdin
        si.hStdOutput = h_stdout
        si.hStdError = h_stdout
        pi = PROCESS_INFORMATION()
        cmd_buf = ctypes.create_unicode_buffer(runner_cmdline)

        # Start the runner as the sandbox user.  CreateProcessWithLogonW needs
        # no privileges; the restricted-token enforcement happens inside the
        # runner before the real command starts.
        _log.info(
            "sandbox spawn attempt user=%s level=%s network=%s runner=%s",
            user,
            level,
            network,
            runner_head[0],
        )
        ok = w["CreateProcessWithLogonW"](
            user,
            ".",  # local account domain
            password,
            0,  # dwLogonFlags: do not load the profile
            None,
            cmd_buf,
            CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
            env_block,
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            for fd in (stdout_r, stdout_w):
                try:
                    os.close(fd)
                except Exception:
                    pass
            if stdin_file is not None:
                try:
                    stdin_file.close()
                except Exception:
                    pass
            if h_job:
                try:
                    w["CloseHandle"](h_job)
                except Exception:
                    pass
            if cmd_file is not None:
                try:
                    os.unlink(cmd_file)
                except OSError:
                    pass
            win_err = ctypes.WinError(ctypes.get_last_error())
            if win_err.winerror == 5:
                # The most common packaged-app failure: the sandbox user could
                # not access the runner image or the working directory.  Give
                # the user actionable hints instead of a bare "Access denied".
                hint = _spawn_access_denied_hint(
                    runner_head[0], cwd
                )
                _log.warning(
                    "sandbox spawn denied: error=5 runner=%s cwd=%s",
                    runner_head[0],
                    cwd,
                )
                raise OSError(
                    win_err.winerror, f"{win_err.strerror}. {hint}"
                )
            raise win_err
        _log.info(
            "sandbox spawn user=%s level=%s network=%s runner_pid=%s",
            user,
            level,
            network,
            int(pi.dwProcessId),
        )

        # Parent-side cleanup: close the inheritable ends we handed to the child.
        try:
            os.close(stdout_w)
        except Exception:
            pass
        if stdin_data is None:
            try:
                os.close(nul_fd)
            except Exception:
                pass
        else:
            try:
                os.close(stdin_r)
            except Exception:
                pass

        stdout_file = os.fdopen(stdout_r, "rb", buffering=0)
        return WindowsSandboxProcess(
            w,
            int(pi.hProcess),
            int(pi.hThread),
            int(h_job) if h_job else 0,
            int(pi.dwProcessId),
            stdout_file,
            stdin_file,
            str(exit_file),
            [str(cmd_file)] if cmd_file is not None else None,
            None,
        )

    # -- provisioning -------------------------------------------------------

    def provision(
        self,
        config_dir: Any,
        workspace_root: Optional[str],
        level: str = "workspace_write",
        progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Idempotent one-time setup. Must run in an elevated process.

        ``progress`` receives a human-readable line as each step starts and
        completes, so the CLI can stream setup progress to the console instead
        of printing everything after the fact.
        """
        steps: list = []
        errors: list = []

        def emit(msg: str) -> None:
            steps.append(msg)
            if progress:
                try:
                    progress("  ✓ " + msg)
                except Exception:
                    pass

        def announce(msg: str) -> None:
            if progress:
                try:
                    progress("  … " + msg)
                except Exception:
                    pass

        announce("preparing sandbox user credentials")
        secret = _ensure_secret(config_dir)

        if _user_exists(SANDBOX_USERS_GROUP):
            emit(f"group {SANDBOX_USERS_GROUP}: exists")
        else:
            announce(f"creating group {SANDBOX_USERS_GROUP}")
            result = _run_process(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "New-LocalGroup -Name '{0}' -ErrorAction Stop".format(
                        SANDBOX_USERS_GROUP
                    ),
                ]
            )
            if result.returncode != 0:
                errors.append(
                    f"create group {SANDBOX_USERS_GROUP}: {result.stderr.strip()}"
                )
            else:
                emit(f"group {SANDBOX_USERS_GROUP}: created")

        # Strip the OLD users' ACLs from every sandbox-managed location BEFORE
        # the accounts are deleted: after Remove-LocalUser their SIDs become
        # unresolvable and the ACEs would linger as stale entries.  The ACL
        # record lists every directory that ever received sandbox ACLs (across
        # all Code Wood data directories), so all of them are swept, not just
        # the current workspace.
        recorded = sorted(_load_acl_record())
        if recorded:
            announce(
                "removing old sandbox-user ACLs from %d recorded directories"
                % len(recorded)
            )
        for recorded_dir in recorded:
            self.cleanup_workspace_acls(recorded_dir, config_dir)
        runtime_root = _shared_sandbox_root()
        if runtime_root.exists():
            for user in (SANDBOX_USER_OFFLINE, SANDBOX_USER_ONLINE):
                _run_process(["icacls", str(runtime_root), "/remove:g", user, "/t", "/q"])
                _run_process(["icacls", str(runtime_root), "/remove:d", user, "/t", "/q"])

        # ``net user`` rejects usernames longer than 20 characters (legacy
        # NetBIOS limit) with a bare usage message, so user management goes
        # through the PowerShell LocalAccounts module which has no such limit.
        offline_created = False
        for user, key in (
            (SANDBOX_USER_OFFLINE, "offline"),
            (SANDBOX_USER_ONLINE, "online"),
        ):
            if _user_exists(user):
                # Recreate instead of refreshing the password: an account
                # created by another Code Wood data directory (or left
                # disabled/locked) can reject Set-LocalUser with
                # InvalidPasswordException, and the account's password must
                # match THIS data directory's secret to keep running sandboxes
                # working after setup.
                announce(f"removing existing user {user} (will be recreated)")
                result = _run_process(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "Remove-LocalUser -Name '{0}' -ErrorAction Stop".format(
                            user
                        ),
                    ]
                )
                if result.returncode != 0:
                    errors.append(
                        f"remove existing user {user}: {result.stderr.strip()}"
                    )
                    continue
                emit(f"user {user}: removed existing account")
            # Create the account, retrying with a fresh password when the
            # local password policy (complexity / history / minimum length)
            # rejects the generated one.  The accepted password is persisted
            # back into the secret file so sandbox logon keeps working.
            password = secret[key]
            create_error: Optional[str] = None
            for attempt in range(4):
                announce(
                    f"creating user {user}"
                    + (f" (attempt {attempt + 1})" if attempt else "")
                )
                ps = (
                    "New-LocalUser -Name '{0}' -Password "
                    "(ConvertTo-SecureString '{1}' -AsPlainText -Force) "
                    "-PasswordNeverExpires -AccountNeverExpires"
                ).format(user, password)
                result = _run_process(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]
                )
                if result.returncode == 0:
                    emit(f"user {user}: created")
                    create_error = None
                    break
                output = (result.stderr or "") + (result.stdout or "")
                if "InvalidPasswordException" in output:
                    # Password policy rejected this password; retry with a
                    # fresh one and grow the length in case the policy sets a
                    # higher minimum.
                    password = _random_password(14 + attempt)
                    create_error = (
                        f"create user {user}: password rejected by local "
                        "password policy; retried with a new password"
                    )
                    continue
                create_error = f"create user {user}: {result.stderr.strip()}"
                break
            if create_error:
                errors.append(create_error)
                continue
            if password != secret[key]:
                secret[key] = password
                _save_secret(config_dir, secret)
            if user == SANDBOX_USER_OFFLINE:
                offline_created = True
            _run_process(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Add-LocalGroupMember -Group 'Users' -Member '{0}' "
                    "-ErrorAction SilentlyContinue".format(user),
                ]
            )
            result = _run_process(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$group='{0}'; $member='{1}'; "
                    "if (-not (Get-LocalGroupMember -Group $group -Member $member "
                    "-ErrorAction SilentlyContinue)) {{ "
                    "Add-LocalGroupMember -Group $group -Member $member -ErrorAction Stop }}"
                    .format(SANDBOX_USERS_GROUP, user),
                ]
            )
            if result.returncode != 0:
                errors.append(
                    f"add {user} to {SANDBOX_USERS_GROUP}: {result.stderr.strip()}"
                )

        # Firewall: block outbound traffic for the offline user. PowerShell is
        # used with the user's SID wrapped in an SDDL authorization list
        # (netsh's ``user=`` is not supported on outbound rules, and a bare
        # SID string is rejected by New-NetFirewallRule). The rule is removed
        # first so provisioning stays idempotent. May be restricted by group
        # policy, in which case file isolation still works but network
        # isolation degrades.
        firewall_ok = False
        if offline_created:
            ps_remove = (
                "Remove-NetFirewallRule -DisplayName '{0}' -ErrorAction SilentlyContinue"
            ).format(SANDBOX_FIREWALL_RULE_OFFLINE)
            announce(f"configuring firewall rule '{SANDBOX_FIREWALL_RULE_OFFLINE}'")
            _run_process(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_remove]
            )
            ps_rule = (
                "$sid=(Get-LocalUser -Name '{0}').SID.Value; "
                "New-NetFirewallRule -DisplayName '{1}' -Direction Outbound "
                "-Action Block -Profile Any -LocalUser ('D:(A;;CC;;;' + $sid + ')') "
                "-ErrorAction Stop"
            ).format(SANDBOX_USER_OFFLINE, SANDBOX_FIREWALL_RULE_OFFLINE)
            result = _run_process(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_rule]
            )
            firewall_ok = result.returncode == 0
            if firewall_ok:
                emit(f"firewall rule '{SANDBOX_FIREWALL_RULE_OFFLINE}': created")
            else:
                errors.append(
                    "firewall rule could not be created: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
        # If the offline user failed to create, its error is already reported
        # above and the cascading firewall failure is skipped.

        # Sandbox runtime dirs (home/temp/AppData) writable by both sandbox users.
        cap_sids = _load_or_create_cap_sids(config_dir)
        sandbox_root = _shared_sandbox_root()
        announce("preparing sandbox runtime directories")
        runtime_dirs = [
            sandbox_root / "home",
            sandbox_root / "tmp",
            sandbox_root / "home" / "AppData" / "Roaming",
            sandbox_root / "home" / "AppData" / "Local",
        ]
        for directory in runtime_dirs:
            directory.mkdir(parents=True, exist_ok=True)
            _run_process(
                ["icacls", str(directory), "/grant", f"{SANDBOX_USERS_GROUP}:(OI)(CI)(M)"]
            )
            # The restricted token can write the runtime dirs only through the
            # capability SIDs (the group ACE is invisible to its write check),
            # so grant both capability SIDs explicitly.
            _ps_grant_modify_sid(str(directory), cap_sids["workspace"])
            _ps_grant_modify_sid(str(directory), cap_sids["readonly"])
        emit("sandbox runtime dirs: ready")

        # Source builds launch the runner with the bundled Python interpreter,
        # so the sandbox users must be able to read it (interpreter + stdlib).
        # Packaged builds use shell-runner.exe and skip this. The runner
        # process runs under the plain logon token, so granting the sandbox
        # users group ReadAndExecute is sufficient. Best-effort and
        # non-fatal: a source user on a locked-down machine can still fix the
        # ACL manually or use CODOWN_SANDBOX_RUNNER_PYTHON.
        if not getattr(sys, "frozen", False):
            announce(
                "granting sandbox users read access to the Python interpreter"
            )
            py_dirs = {
                Path(sys.executable).resolve().parent,
                Path(sys.prefix),
                Path(sys.base_prefix),
            }
            py_ok = True
            for py_dir in sorted(py_dirs, key=str):
                if not py_dir.is_dir():
                    continue
                try:
                    if _ps_grant_read_group(str(py_dir)) != 0:
                        py_ok = False
                except Exception:
                    py_ok = False
            emit(
                "python interpreter read: "
                + ("granted" if py_ok else "failed (non-fatal for source runs)")
            )

        announce("applying workspace ACLs")
        self.apply_workspace_acls(workspace_root, level, config_dir)
        emit(f"workspace ACLs: {level}")

        flag = _flag_path(config_dir)
        announce("writing provisioning flag")
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(
            json.dumps(
                {
                    "level": level,
                    "provisioned": True,
                    "firewall_ok": bool(firewall_ok),
                }
            ),
            encoding="utf-8",
        )
        emit("provisioning flag: written")

        ok = not errors
        if ok:
            # Credentials may have changed; drop the cached check so the
            # settings page re-verifies on the next load.
            _credential_check_cache.pop(str(Path(config_dir).resolve()), None)
        return {
            "ok": ok,
            "steps": steps,
            "errors": errors,
            "message": (
                "Sandbox provisioning complete."
                if ok
                else "Sandbox provisioning finished with errors."
            ),
        }

    def apply_workspace_acls(
        self,
        workspace_root: Optional[str],
        level: str,
        config_dir: Any = None,
    ) -> None:
        """Grant/revoke sandbox-group writes on the workspace (no elevation).

        ``read_only`` grants the sandbox user group read+execute only; any
        other level grants it modify. Protected subdirectories (``.git``, the
        workspace config dir) always get an explicit write-deny.

        All edits are batched into a single PowerShell invocation — each
        ``powershell.exe`` costs ~0.5-1.5s to start, and the previous
        per-ACE helpers spawned ~25 processes for this step. The script also
        skips ``Set-Acl`` when the managed ACE set is already correct, so
        repeated refreshes do not re-propagate inheritance over the tree.
        """
        if not workspace_root:
            return
        ws = str(Path(workspace_root).resolve())
        # Remember the directory so a future user rebuild strips its ACLs too.
        _record_acl_dirs([ws])
        cap_sids = _load_or_create_cap_sids(config_dir) if config_dir else None
        protected = []
        for sub in (".git", ".codewood", ".agents"):
            target_path = Path(ws) / sub
            if target_path.exists():
                protected.append(str(target_path))
        script = _build_workspace_acl_script(
            ws,
            "readonly" if level == "read_only" else "write",
            SANDBOX_USERS_GROUP,
            (SANDBOX_USER_OFFLINE, SANDBOX_USER_ONLINE),
            cap_sids,
            protected,
        )
        result = _run_process(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        )
        if result.returncode != 0:
            _log.warning(
                "workspace ACL apply failed: %s",
                (result.stderr or result.stdout or "").strip(),
            )

    def cleanup_workspace_acls(
        self, workspace_root: Optional[str], config_dir: Any = None
    ) -> None:
        """Strip every sandbox-managed ACE from a workspace tree (best effort).

        Called BEFORE the sandbox users are deleted/recreated during
        provisioning (so the old accounts still resolve by name) and when a
        workspace is deleted. Removes:

        - the two sandbox users and the sandbox group by name, recursively
          (``icacls /t``);
        - the capability SIDs and any dead (no longer resolvable) local SIDs
          at the root via PowerShell — inheritable ACE removal propagates to
          children through auto-inheritance.

        No elevation needed (the files belong to the current user).
        """
        if not workspace_root:
            return
        # The directory no longer carries sandbox-managed ACLs: drop it from
        # the record even if it no longer exists (nothing left to clean).
        _unrecord_acl_dirs([workspace_root])
        ws = Path(workspace_root)
        if not ws.exists():
            return
        ws_str = str(ws.resolve())
        for ident in (
            SANDBOX_USER_OFFLINE,
            SANDBOX_USER_ONLINE,
            SANDBOX_USERS_GROUP,
        ):
            _run_process(["icacls", ws_str, "/remove:g", ident, "/t", "/q"])
            _run_process(["icacls", ws_str, "/remove:d", ident, "/t", "/q"])
        cap_w = cap_r = ""
        if config_dir:
            try:
                cap = _load_or_create_cap_sids(config_dir)
                cap_w = cap.get("workspace", "")
                cap_r = cap.get("readonly", "")
            except Exception:
                pass
        _run_process(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _build_cleanup_root_script(ws_str, cap_w, cap_r),
            ]
        )


def launch_elevated_setup(
    config_dir: Any, workspace_root: Optional[str], level: str
) -> bool:
    """Relaunch ``cli.main sandbox setup`` elevated via UAC (ShellExecuteW)."""
    try:
        import cli.main as cli_main

        main_py = Path(cli_main.__file__).resolve()
        tail = [
            "sandbox",
            "setup",
            "--gui",
            "--config-dir",
            str(config_dir),
            "--workspace",
            str(workspace_root or ""),
            "--level",
            level,
        ]
        if getattr(sys, "frozen", False):
            # Frozen builds: sys.executable IS the app (codewood.exe), which
            # hands the remaining argv straight to cli.main. The cli/main.py
            # script argument must not be included, or the exe treats it as a
            # regular argv[0] and never reaches the "sandbox" dispatcher.
            args = tail
        else:
            args = [str(main_py)] + tail
        params = subprocess.list2cmdline(args)
        result = _win()["ShellExecuteW"](
            None, "runas", sys.executable, params, None, SW_SHOWNORMAL
        )
        return int(result) > 32
    except Exception:
        return False
