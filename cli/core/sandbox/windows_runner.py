"""Sandbox command runner (runs as the sandbox user).

The parent backend launches this module with ``CreateProcessWithLogonW`` under
the sandbox user.  This process then derives a ``WRITE_RESTRICTED`` restricted
token from its **own** primary token (the sandbox user's), and launches the real
command with ``CreateProcessAsUserW``.  Because the token is a restricted
version of the caller's primary token, no ``SeAssignPrimaryTokenPrivilege`` is
required -- unlike doing the same from the real user's process, which fails
with ERROR_PRIVILEGE_NOT_HELD (1314).

The child inherits this process's stdin/stdout/stderr, environment and working
directory, so the parent only hands over inherited handle *values* on the
command line (``CreateProcessWithLogonW`` always inherits inheritable handles).

Argv:
  --cmd <cmdline>   command line to execute (already ``cmd /c``-wrapped)
  --cap <cap-sid>   capability SID that may write the workspace
  --exit <handle>   inherited pipe handle; the child exit code is written here
  [--hpc <handle>]  ConPTY handle (decimal) inherited from the parent
  [--job <handle>]  job object handle (decimal); child is assigned to it

The runner prints nothing: stdout/stderr are the child's data channels.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import os
import struct

DISABLE_MAX_PRIVILEGE = 0x1
LUA_TOKEN = 0x4
WRITE_RESTRICTED = 0x8
TOKEN_USER_CLASS = 1
TOKEN_GROUPS_CLASS = 2
TOKEN_DEFAULT_DACL_CLASS = 6
SE_GROUP_LOGON_ID = 0xC0000000
SE_PRIVILEGE_ENABLED = 0x2
GENERIC_ALL = 0x10000000
ACL_REVISION = 2
WIN_WORLD_SID = 1
STARTF_USESTDHANDLES = 0x00000100
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", w.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
        ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
        ("lpReserved2", ctypes.POINTER(w.BYTE)), ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", w.HANDLE), ("hThread", w.HANDLE),
        ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD),
    ]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", w.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", w.DWORD), ("Privileges", LUID_AND_ATTRIBUTES)]


class TOKEN_DEFAULT_DACL(ctypes.Structure):
    _fields_ = [("DefaultDacl", ctypes.c_void_p)]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", w.DWORD),
        ("TrusteeForm", w.DWORD),
        ("TrusteeType", w.DWORD),
        ("ptstrName", ctypes.c_void_p),
    ]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", w.DWORD),
        ("grfAccessMode", w.DWORD),
        ("grfInheritance", w.DWORD),
        ("Trustee", TRUSTEE_W),
    ]


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)


def _configure():
    k32.OpenProcessToken.restype = w.BOOL
    k32.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    k32.GetCurrentProcess.restype = w.HANDLE
    k32.CloseHandle.restype = w.BOOL
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.WaitForSingleObject.restype = w.DWORD
    k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k32.GetExitCodeProcess.restype = w.BOOL
    k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    k32.GetStdHandle.restype = w.HANDLE
    k32.GetStdHandle.argtypes = [w.DWORD]
    k32.WriteFile.restype = w.BOOL
    k32.WriteFile.argtypes = [
        w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p,
    ]
    k32.LocalFree.restype = w.HLOCAL
    k32.LocalFree.argtypes = [w.HLOCAL]
    k32.CreateFileW.restype = w.HANDLE
    k32.CreateFileW.argtypes = [
        w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE,
    ]
    k32.InitializeProcThreadAttributeList.restype = w.BOOL
    k32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.UpdateProcThreadAttribute.restype = w.BOOL
    k32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    k32.DeleteProcThreadAttributeList.restype = None
    k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k32.AssignProcessToJobObject.restype = w.BOOL
    k32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k32.CreatePseudoConsole.restype = ctypes.c_long
    k32.CreatePseudoConsole.argtypes = [
        COORD, w.HANDLE, w.HANDLE, w.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    k32.ClosePseudoConsole.restype = None
    k32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]

    adv.GetTokenInformation.restype = w.BOOL
    adv.GetTokenInformation.argtypes = [
        w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
    ]
    adv.SetTokenInformation.restype = w.BOOL
    adv.SetTokenInformation.argtypes = [
        w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
    ]
    adv.CreateRestrictedToken.restype = w.BOOL
    adv.CreateRestrictedToken.argtypes = [
        w.HANDLE, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p,
        w.DWORD, ctypes.c_void_p, ctypes.POINTER(w.HANDLE),
    ]
    adv.CreateProcessAsUserW.restype = w.BOOL
    adv.CreateProcessAsUserW.argtypes = [
        w.HANDLE, w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL,
        w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    adv.CreateWellKnownSid.restype = w.BOOL
    adv.CreateWellKnownSid.argtypes = [
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(w.DWORD),
    ]
    adv.ConvertStringSidToSidW.restype = w.BOOL
    adv.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.restype = w.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetLengthSid.restype = w.DWORD
    adv.GetLengthSid.argtypes = [ctypes.c_void_p]
    adv.CopySid.restype = w.BOOL
    adv.CopySid.argtypes = [w.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    adv.InitializeAcl.restype = w.BOOL
    adv.InitializeAcl.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD]
    adv.AddAccessAllowedAce.restype = w.BOOL
    adv.AddAccessAllowedAce.argtypes = [
        ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p,
    ]
    adv.LookupPrivilegeValueW.restype = w.BOOL
    adv.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(LUID)]
    adv.AdjustTokenPrivileges.restype = w.BOOL
    adv.AdjustTokenPrivileges.argtypes = [
        w.HANDLE, w.BOOL, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
    ]
    adv.GetSecurityInfo.restype = w.DWORD
    adv.GetSecurityInfo.argtypes = [
        w.HANDLE, ctypes.c_int, w.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ]
    adv.SetSecurityInfo.restype = w.DWORD
    adv.SetSecurityInfo.argtypes = [
        w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    adv.SetEntriesInAclW.restype = w.DWORD
    adv.SetEntriesInAclW.argtypes = [
        w.DWORD, ctypes.POINTER(EXPLICIT_ACCESS_W), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]


_configure()


def _token_info(h_token: int, cls: int):
    size = w.DWORD()
    adv.GetTokenInformation(h_token, cls, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    adv.GetTokenInformation(h_token, cls, buf, size.value, ctypes.byref(size))
    return buf


def _copy_sid(sid: int, keep: list) -> int:
    length = adv.GetLengthSid(sid)
    out = ctypes.create_string_buffer(length)
    adv.CopySid(length, out, sid)
    keep.append(out)
    return ctypes.cast(out, ctypes.c_void_p).value or 0


def _sid_from_token_groups(buf, keep: list) -> int:
    count = struct.unpack_from("<I", buf.raw, 0)[0]
    off = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4
    for i in range(count):
        entry = SID_AND_ATTRIBUTES.from_buffer_copy(
            buf.raw, off + i * ctypes.sizeof(SID_AND_ATTRIBUTES)
        )
        if entry.Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
            return _copy_sid(entry.Sid, keep)
    return 0


def _world_sid(keep: list) -> int:
    length = w.DWORD()
    adv.CreateWellKnownSid(WIN_WORLD_SID, None, None, ctypes.byref(length))
    buf = ctypes.create_string_buffer(length.value)
    adv.CreateWellKnownSid(WIN_WORLD_SID, None, buf, ctypes.byref(length))
    keep.append(buf)
    return ctypes.cast(buf, ctypes.c_void_p).value or 0


def _enable_change_notify(h_token: int) -> None:
    luid = LUID()
    if not adv.LookupPrivilegeValueW(None, "SeChangeNotifyPrivilege", ctypes.byref(luid)):
        return
    tp = TOKEN_PRIVILEGES()
    tp.PrivilegeCount = 1
    tp.Privileges.Luid = luid
    tp.Privileges.Attributes = SE_PRIVILEGE_ENABLED
    adv.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), ctypes.sizeof(TOKEN_PRIVILEGES), None, None)


def _set_default_dacl(h_token: int, sids: list) -> None:
    """Permissive default DACL so the child can create pipes/IPC objects."""
    size = 64 + 8 * len(sids) + 32 * len(sids)
    acl = ctypes.create_string_buffer(size)
    adv.InitializeAcl(acl, size, ACL_REVISION)
    for sid in sids:
        adv.AddAccessAllowedAce(acl, ACL_REVISION, GENERIC_ALL, sid)
    info = TOKEN_DEFAULT_DACL(ctypes.cast(acl, ctypes.c_void_p))
    ret = w.DWORD()
    adv.SetTokenInformation(
        h_token, TOKEN_DEFAULT_DACL_CLASS, ctypes.byref(info),
        ctypes.sizeof(TOKEN_DEFAULT_DACL), ctypes.byref(ret),
    )


def _allow_null_device(cap_sid: int) -> None:
    """Grant the capability SID read/write/execute on \\\\.\\NUL so redirects work."""
    h = k32.CreateFileW(r"\\.\NUL", 0x00020000 | 0x00040000, 3, None, 3, 0x80, None)
    if not h or h == w.HANDLE(-1).value:
        return
    try:
        owner = ctypes.c_void_p()
        group = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        sacl = ctypes.c_void_p()
        sd = ctypes.c_void_p()
        if adv.GetSecurityInfo(
            h, 6, 0x4, ctypes.byref(owner), ctypes.byref(group),
            ctypes.byref(dacl), ctypes.byref(sacl), ctypes.byref(sd),
        ) != 0:
            return
        new_dacl = ctypes.c_void_p()
        entries = (EXPLICIT_ACCESS_W * 1)()
        entries[0].grfAccessPermissions = (
            0x120089 | 0x120116 | 0x1200A0
        )  # FILE_GENERIC_READ|WRITE|EXECUTE
        entries[0].grfAccessMode = 2  # SET_ACCESS
        entries[0].Trustee.TrusteeForm = 1  # TRUSTEE_IS_SID
        entries[0].Trustee.TrusteeType = 8  # TRUSTEE_IS_UNKNOWN
        entries[0].Trustee.ptstrName = ctypes.c_void_p(cap_sid)
        if adv.SetEntriesInAclW(1, entries, dacl, ctypes.byref(new_dacl)) == 0:
            adv.SetSecurityInfo(h, 6, 0x4, None, None, new_dacl, None)
            if new_dacl.value:
                k32.LocalFree(new_dacl)
        if sd.value:
            k32.LocalFree(sd)
    finally:
        k32.CloseHandle(h)


def _make_restricted_token(cap_sid_str: str) -> tuple:
    """Return (token_handle, [keep-buffers]) with the Codex-style restricted token."""
    keep: list = []
    TOKEN_ALL_ACCESS_BITS = (
        0x0008 | 0x0002 | 0x0001 | 0x0080 | 0x0020
    )  # QUERY|DUPLICATE|ASSIGN_PRIMARY|ADJUST_DEFAULT|ADJUST_PRIVILEGES
    h_self = w.HANDLE()
    if not k32.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_ALL_ACCESS_BITS, ctypes.byref(h_self)):
        return 0, keep
    user_buf = _token_info(h_self, TOKEN_USER_CLASS)
    user_sid = _copy_sid(SID_AND_ATTRIBUTES.from_buffer(user_buf).Sid, keep)
    groups_buf = _token_info(h_self, TOKEN_GROUPS_CLASS)
    logon_sid = _sid_from_token_groups(groups_buf, keep) or user_sid
    everyone = _world_sid(keep)
    pcap = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(cap_sid_str, ctypes.byref(pcap)):
        return 0, keep
    cap = pcap.value or 0
    restrict = (SID_AND_ATTRIBUTES * 4)(
        SID_AND_ATTRIBUTES(cap, 0),
        SID_AND_ATTRIBUTES(user_sid, 0),
        SID_AND_ATTRIBUTES(logon_sid, 0),
        SID_AND_ATTRIBUTES(everyone, 0),
    )
    h_restricted = w.HANDLE()
    ok = adv.CreateRestrictedToken(
        h_self, DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED,
        0, None, 0, None, 4, restrict, ctypes.byref(h_restricted),
    )
    k32.CloseHandle(h_self)
    if not ok:
        return 0, keep
    _set_default_dacl(h_restricted.value, [logon_sid, everyone, cap])
    _enable_change_notify(h_restricted.value)
    _allow_null_device(cap)
    return h_restricted.value, keep


def _make_startup() -> tuple:
    """Build STARTUPINFO(EX); prefer a ConPTY bound to our inherited std handles."""
    h_in = k32.GetStdHandle(STD_INPUT_HANDLE)
    h_out = k32.GetStdHandle(STD_OUTPUT_HANDLE)
    h_err = k32.GetStdHandle(STD_ERROR_HANDLE)
    # Default to the plain-pipe fallback: ConPTY output forwarding proved
    # unreliable under the LogonW-launched runner (the child runs fine, but its
    # screen writes are silently lost), while STARTF_USESTDHANDLES pipes
    # capture stdout/stderr correctly. ConPTY stays available behind an
    # explicit env opt-in for interactive full-screen programs.
    hpc = None
    if os.environ.get("CODOWN_SANDBOX_USE_CONPTY"):
        hpc = ctypes.c_void_p()
        ok = k32.CreatePseudoConsole(COORD(120, 30), h_in, h_out, 0, ctypes.byref(hpc))
        if ok == 0 and hpc.value:
            size = ctypes.c_size_t()
            k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            attr = ctypes.cast(buf, ctypes.c_void_p)
            if k32.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size)):
                if k32.UpdateProcThreadAttribute(
                    attr, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                    ctypes.byref(hpc), ctypes.sizeof(hpc), None, None,
                ):
                    si_ex = STARTUPINFOEXW()
                    si_ex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
                    si_ex.lpAttributeList = attr
                    return (
                        ctypes.byref(si_ex),
                        CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
                        buf,
                        hpc,
                    )
            k32.ClosePseudoConsole(hpc)
        hpc = None
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdInput = h_in
    si.hStdOutput = h_out
    si.hStdError = h_err
    return ctypes.byref(si), CREATE_UNICODE_ENVIRONMENT, None, None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cmd", required=True)
    parser.add_argument("--cap", required=True)
    parser.add_argument("--exit-file", required=True)
    parser.add_argument("--job", type=int, default=0)
    args = parser.parse_args(argv)

    h_token, _keep = _make_restricted_token(args.cap)
    if not h_token:
        _write_exit_file(args.exit_file, 0xFFFFFFFF)
        return 2
    try:
        startup, flags, attr_buf, hpc = _make_startup()
        cmd_buf = ctypes.create_unicode_buffer(args.cmd)
        pi = PROCESS_INFORMATION()
        # bInheritHandles depends on the startup path: the ConPTY path
        # (PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE) requires FALSE — with TRUE the
        # child inherits the pseudo-console handle itself and its output is
        # silently lost. The plain-pipe fallback (STARTF_USESTDHANDLES)
        # requires TRUE, otherwise the child's std handles are invalid.
        inherit = not bool(hpc)
        ok = adv.CreateProcessAsUserW(
            h_token, None, cmd_buf, None, None, inherit, flags, None, None,
            startup, ctypes.byref(pi),
        )
        if not ok:
            _write_exit_file(args.exit_file, 0xFFFFFFFF)
            return 4
        try:
            if args.job:
                k32.AssignProcessToJobObject(args.job, pi.hProcess)
            k32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
            code = w.DWORD()
            k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
            _write_exit_file(args.exit_file, code.value)
        finally:
            k32.CloseHandle(pi.hProcess)
            k32.CloseHandle(pi.hThread)
        if hpc:
            k32.ClosePseudoConsole(hpc)
        return 0
    finally:
        k32.CloseHandle(h_token)


def _write_exit_file(path: str, code: int) -> None:
    with open(path, "wb") as f:
        f.write(struct.pack("<I", code))


if __name__ == "__main__":
    raise SystemExit(main())
