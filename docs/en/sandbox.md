# Shell sandbox

Code Wood can run AI-issued `shell` commands behind an operating-system-level
isolation layer that limits their access to the filesystem and the network. The
design follows OpenAI's
[Building a Codex sandbox for Windows](https://openai.com/index/building-codex-windows-sandbox/).

## Levels

| Level | File writes | File reads | Network |
| --- | --- | --- | --- |
| `read_only` | Denied (including the workspace) | Depends on the read permissions granted to the sandbox user | Denied |
| `workspace_write` | The current workspace and the real `%TEMP%` directory; protected directories such as `.git` / `.codewood` excluded | Depends on the read permissions granted to the sandbox user | Toggleable |
| `full_access` | Same as the current user | Same as the current user | Allowed |

The default level is `full_access`, which preserves the existing behavior.

> **Readable ≠ writable.** Read access depends on the permissions granted to the
> sandbox user identity; outside the workspace a path may be readable but is
> never writable. The sandbox is not a data-leak-prevention system and is not
> equivalent to a virtual machine.

## Windows implementation

As with Codex, the Windows sandbox is not a single "sandbox program" but a
combination of several native Windows security mechanisms. Command execution
uses a **runner process** architecture (modeled on Codex's
`window-sandbox-rs`): the main process starts a **runner** as the sandbox user
via `CreateProcessWithLogonW`, and the runner then spawns the real command with a
**restricted token** via `CreateProcessAsUserW`:

```text
main process ──CreateProcessWithLogonW──▶ runner (sandbox user)
                                           │ CreateRestrictedToken
                                           │   restricted SIDs = [capability SID,
                                           │     user SID, logon SID, Everyone]
                                           ▼ CreateProcessAsUserW
                                        real command (restricted token)
```

The runner itself runs with a **normal logon token** (so granting the sandbox
user group read access is enough); only the real command holds the restricted
token, and its file-access checks match precisely against the restricted SID set.

1. **Dedicated local users and group** `CodewoodSandOffline` / `CodewoodSandOnline`
   provide the filesystem identity (Windows local account names are limited to
   20 characters, hence the shortened "Sand" infix). The Offline user is matched
   by an outbound BLOCK firewall rule and the Online user is unrestricted, so the
   network switch never has to rewrite rules at runtime. Both accounts belong to
   the `CodewoodSandUsers` local group.
2. **Capability SID allowlist** (the Codex approach): during provisioning, two
   random `S-1-5-21-*` capability SIDs (`workspace` / `readonly`) are persisted to
   `%LOCALAPPDATA%/<app name>/sandbox/sandbox_cap_sid.json`. The restricted token
   adds the corresponding capability SID to its restricted SIDs, and **write
   access is granted only through that SID** — so even if a directory grants
   `Modify` to groups such as `Authenticated Users`, a restricted command still
   cannot write outside the workspace. The workspace grants `Modify` only to the
   `workspace` capability SID (with explicit `DENY` on protected subdirectories),
   and the `read_only` level issues a write `DENY` for both capability SIDs.
   At the `workspace_write` level, the real `%TEMP%` directory (the real path
   preserved for sandboxed commands, see "Process environment") is likewise
   granted `Modify` through the `workspace` capability SID, so temporary-file
   reads and writes do not fail because of the sandbox; switching back to
   `read_only` revokes that grant.
3. **Workspace ACL**: applied automatically when a workspace is switched,
   opened, or started (see below); per-user ACEs left over from older versions
   are purged so stale permissions do not accumulate. Workspace roots that have
   had ACLs applied are recorded in `sandbox_acl_dirs.json` in the shared state
   directory; when the sandbox is re-provisioned, every historical directory in
   that list has its ACL cleaned up, and deleting a workspace removes it from the
   list as well.
4. **Windows Firewall**: an outbound BLOCK rule is created for the Offline user,
   blocking all outbound connections including HTTP/TCP/UDP (application-layer
   proxy or environment-variable approaches are deliberately avoided because
   programs that do not honor the convention can bypass them).
5. **Job Object (`KILL_ON_JOB_CLOSE`)**: the runner assigns the real command to a
   job, so when a sandboxed command is interrupted or times out the entire
   process tree is terminated together.
6. **DPAPI**: the sandbox user passwords are encrypted with `CryptProtectData`
   and stored at `%LOCALAPPDATA%/<app name>/sandbox/sandbox_secret.bin`.

> **Shared state directory**: all sandbox state (secrets, provisioning marker,
> capability SIDs, runtime directory, ACL records) lives in
> `%LOCALAPPDATA%/<app name>/sandbox/`, independent of any `config` directory.
> Multiple Code Wood instances on the same machine share one set of sandbox users
> and passwords, so rebuilding users no longer affects the others. After an
> upgrade, the first provisioning run recreates the users and adopts the shared
> secret (stale sandbox files in old config directories can be deleted manually).

### How the runner is started

- **Running from source**: spawn loads `cli/core/sandbox/windows_runner.py` with
  the current interpreter (`sys.executable`, overridable via
  `CODOWN_SANDBOX_RUNNER_PYTHON`) using `-c`. Provisioning grants the sandbox
  user read access to the interpreter directory (see below).
- **Packaged build (PyInstaller)**: the artifact `codewood\shell-runner.exe`
  shares the same `_internal\` runtime as the main program and is built together
  by `build/codewood.spec`; spawn starts that exe directly as the sandbox user and
  does not depend on any Python interpreter.

### Process environment

Sandboxed commands **inherit the real user's environment**: `HOME` /
`USERPROFILE` / `TEMP` / `APPDATA` keep their real paths and are not redirected
(provisioning creates `sandbox/home` and `sandbox/tmp` as the sandbox's own
runtime directories, e.g. for runner scripts and exit-code files). Paths outside
the workspace are readable by default (relying on the user-directory read
permissions granted at provisioning) but not writable.

At the `workspace_write` level, the real `%TEMP%` directory (the `%TEMP%`
sandboxed commands see) additionally gains write access: an inheritable `Modify`
is granted to the temporary directory root through the `workspace` capability
SID, and the `read_only` level revokes that grant and restores the original
state. npm / pip / compilers can therefore read and write temporary files without
failing because of the sandbox, while every other path outside the workspace
stays read-only.

## Provisioning (one-time, requires administrator)

Creating the users and firewall rules requires administrator privileges. Two ways:

```text
codewood sandbox setup
```

Run it in an administrator terminal; or open the GUI's **Settings → Security →
Sandbox settings** and click **Set up sandbox** (which raises a UAC prompt).
Provisioning is idempotent and can be re-run.

```text
codewood sandbox status
```

Shows the current provisioning state.

When the settings page (Settings → Security) loads, it uses `LogonUserW` to check
that the two sandbox users' passwords still match the current data directory's
secret (cached for 60 seconds, to avoid repeated failed logons triggering account
lockout). If the users exist but their passwords do not match, the page shows a
notice and the "Set up sandbox" entry, and re-running provisioning fixes it. Once
provisioning completes (the marker file is rewritten), the page bypasses the
cache and re-validates immediately, so the button and the error notice disappear.

Provisioning steps:

1. Generate or read the DPAPI-encrypted random password.
2. Create `CodewoodSandOffline` and `CodewoodSandOnline` with PowerShell
   `New-LocalUser` and add them to the `Users` group; if an account already
   exists it is removed with `Remove-LocalUser` and recreated (accounts left over
   from another data directory may refuse a password refresh, and the password
   must match the current data directory's secret). The generated password always
   contains upper case, lower case, digits, and symbols to satisfy password
   complexity policies; if a local policy rejects it (complexity / history /
   minimum length), a new password is generated, retried, and written back to the
   secret file. Before deleting an old account, ACLs referencing the old user are
   recursively cleaned from the workspace directory tree and the sandbox runtime
   directory, and unresolvable (stale) local SIDs are removed from the root
   directory so that no invalid entries remain after the user is recreated.
3. Create an outbound BLOCK rule for the Offline user with PowerShell
   `New-NetFirewallRule` (falling back to `netsh` on failure).
4. Create the sandbox runtime directories (home/tmp/AppData) and grant access.
5. Generate or read the capability SIDs and grant access to the runtime
   directories.
6. When running from source, grant the sandbox user read access to the Python
   interpreter directory (skipped in packaged builds; failures do not block
   provisioning and are best-effort).
7. Apply the workspace ACL for the current level (this part needs no
   administrator, since the files belong to the user; all ACL edits are merged
   into a single PowerShell process and skipped when the ACLs are already
   correct).

When a workspace is deleted (removed from the registry), the ACLs for the sandbox
users/group/capability SIDs on that workspace's directory tree are cleaned up at
the same time, revoking the sandbox's access to a directory you no longer track.

Switching levels and switching, opening, or starting a workspace automatically
refreshes the workspace ACL (a non-elevated path, via
`cli.core.sandbox.refresh_workspace_acls`).

## Behavior when not provisioned (fail closed)

If `sandbox_level` is configured as `read_only` / `workspace_write` but the
sandbox has not been provisioned, `shell` commands are rejected with an explicit
error instead of silently degrading to unsandboxed execution. Configuring
`full_access` requires no provisioning.

## Failure detection and escalation (bypass_sandbox)

### Stating explicitly whether the sandbox caused a failure

Every `shell` command executed at a sandbox level returns sandbox context in its
result so the model can judge the runtime environment:

- `sandbox_level` / `sandbox_network`: the isolation level and network switch for
  this command.
- `sandbox_bypassed: true`: the command was executed with full permissions with
  user approval (one time only).

When a sandboxed command **fails** and looks like it was blocked by the sandbox,
the result additionally carries:

- `sandbox_related: true`: the failure was very likely caused by sandbox
  restrictions (e.g. access denied, network disabled, sandbox startup failure)
  rather than by the command itself.
- `sandbox_reason`: one of `access_denied` / `network_blocked` / `spawn_failure`
  / `not_provisioned`.
- `sandbox_escalation_hint`: guidance text for the model, suggesting it first
  reflect on whether the command is necessary and whether a sandbox-safe
  alternative exists, and how to request escalation if it truly is needed.

Detection criteria (only when the sandbox plan applies):

- A sandbox process startup failure (`spawn_failure`) or missing provisioning
  (`not_provisioned`) is a definite signal;
- Windows exit code `5` (ERROR_ACCESS_DENIED), or output containing
  "Access is denied" / "permission denied" / EACCES etc. → `access_denied`;
- `sandbox_network=false` with a network-type command (git fetch/clone/pull,
  curl, pip/npm install, etc.) whose output contains network unreachable or
  timeout errors → `network_blocked`.

### One-time model-initiated escalation (requires user approval)

The `shell` tool accepts an optional `bypass_sandbox` (boolean) parameter. After
a failure caused by the sandbox and a moment of reflection, the model may, if it
truly is necessary, call `shell` again with `bypass_sandbox: true` to request
executing that command with full permissions (this time only, without changing
the sandbox configuration). A confirmation is always shown before execution, and
the user has three choices:

| User choice | Behavior |
| --- | --- |
| Yes (approve) | The command runs once with full permissions; the result is marked `sandbox_bypassed: true` |
| No (reject) | The current task ends: the command does not run, `user_cancelled` is returned, the runtime treats it as a user cancellation, and the model must not retry or request escalation again |
| Reject with additional information | The task continues: the command does not run, `user_rejected_with_supplement` + `user_supplement` is returned, and the model adjusts its steps based on the user's feedback before continuing |

An escalation approval counts as **the single human review gate for that command**:

- After approval, the regular command-execution confirmation (execution policy)
  is **not** shown a second time; the command runs directly;
- That tool call **skips the automatic AI review** (`_freedom_auto_confirm`),
  because the user already reviewed it manually in the escalation confirmation;
- The skips above only apply when the sandbox is actually active (`read_only` /
  `workspace_write` on a supported platform); with `full_access`, or on an
  unsupported platform, `bypass_sandbox` is a no-op and AI review plus the usual
  confirmation still apply.

> Escalation is an **explicitly user-approved** exception channel: it affects only
> the single approved command, does not change `sandbox_level` in
> `config.jsonc`, and does not let later commands bypass the sandbox.

## Configuration (`config.jsonc`)

```jsonc
{
  "sandbox_level": "full_access",      // read_only | workspace_write | full_access
  "sandbox_network": true               // whether workspace_write allows the network
}
```

## Platform support

`cli/core/sandbox/` provides a platform abstraction (`SandboxBackend`). Windows is
the only implementation today; macOS / Linux return `supported=False` and the
settings page shows "Sandboxing is not supported on this platform yet". seatbelt /
bubblewrap backends can be implemented later behind the same interface.

## Boundaries and limitations

- The sandbox only constrains the AI's `shell` tool; in-process file tools such
  as `apply_patch` / `read` / grep are still governed by the existing path
  policy, and the GUI's embedded terminal is used directly by the user and is not
  sandboxed.
- Read scope depends on the permissions granted to the sandbox user identity:
  files under the user's home directory (`C:\Users\...`) are usually not readable
  (which is exactly the intended security boundary).
- Protected directories (`.git`, `.codewood`) are not writable inside the
  sandbox — for example commands that need to write `.git`, such as `git commit`,
  fail at a sandbox level; switch to `full_access` instead.
- `read_only` cannot prevent **deletion** (`DEL` requires the `DELETE` permission,
  and adding a `DENY` for it would break `cmd`'s built-in reads); it also cannot
  prevent modifications to existing files that **do not inherit their parent
  directory's ACL** (such files' permissions come from their own ACL). Both are
  treated as known limitations at the documentation level, while the core
  semantics (no creating or writing files) are guaranteed.
- Inside the sandbox, `git` is given `safe.directory=*` through an environment
  variable to avoid false cross-user "dubious ownership" reports.
- **Best-effort, not a security boundary**: this implementation is a best-effort
  approach to command-level isolation. The restricted token blocks filesystem
  writes beyond the workspace by commands "executed as the sandbox user";
  in-process tools (`apply_patch` / `read`), GUI operations, and programs with a
  privilege-escalation path remain outside this mechanism. Do not treat it as a
  sandbox for untrusted malicious code.
- Workspace ACL changes remain in the file permissions; to clean them up, revoke
  the corresponding ACEs manually with `icacls`.
- If group policy forbids creating firewall rules, file isolation still applies
  but network isolation degrades (the provisioning result records the error).
- The sandbox is not a VM: it does not isolate session resources such as the GPU
  or the clipboard, and it does not guarantee data-leak prevention.
