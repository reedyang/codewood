---
name: clawhub-skill-installer
description: Search skills from ClawHub and install a selected skill into config_dir/skills with explicit user confirmation. Use whenever the user asks to browse/find/install skills from clawhub.ai, especially for "检索并安装 skill". Installation must stop on name conflicts with currently loaded skills.
license: Proprietary
---

# ClawHub Skill Installer (built-in)

## Purpose

- Search skill entries from `https://clawhub.ai/skills`
- Install selected skill into `<config_dir>/skills/<skill_id>/`
- Enforce explicit confirmation before installation
- Abort installation when conflict with currently loaded skill is detected

## CLI

Use bundled script:

```text
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" search --query "<keyword>" [--insecure|--no-verify]
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" install --detail-url "<skill detail url>" --config-dir "<config dir>" --confirm "YES" [--insecure|--no-verify]
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" install --query "<keyword>" --config-dir "<config dir>" --confirm "YES" [--max-results 8] [--insecure|--no-verify]
```

## Workflow constraints

- `install` requires `--confirm YES`; otherwise script exits without writing files.
- For slash route `/clawhub-skill-installer <text>`, always treat `<text>` as the query.
- Never reuse prior-turn selection/index/detail_url.
- Selection and confirmation must happen inside installer script terminal prompts.
- Do not ask chat-level index/YES/NO.
- Do not run placeholder shell commands (`echo waiting`, etc.).
- Task boundary is installation only; do not switch to unrelated tasks.

## Mandatory routing for slash usage

When user input is slash-routed as:

- `/clawhub-skill-installer <text>`

the `<text>` part MUST be treated as the initial search query immediately.

Required behavior:

1. Do not ask for another keyword if `<text>` is non-empty.
2. Run installer interactively once (single command):

```text
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" install --query "<text>" --config-dir "<config dir>" --confirm "YES"
```

3. Do NOT run `search --query` before install in slash route; install already contains interactive candidate selection and will print the list once.
4. For slash route `/clawhub-skill-installer <text>`, MUST NOT use `--detail-url` default selection.
5. `--detail-url` is allowed only when user explicitly provides that URL in current message.
6. Do not perform non-installer actions until install finishes or user cancels.
7. If `<text>` itself is a ClawHub detail URL (e.g. `https://clawhub.ai/skills/...`), skip search/index selection and install this URL directly.

## Interactive inputs (script-side)

- Install confirmation prompt: `Confirm installation Yes(y)/No(n):` (input `y/n`).
- Conflict prompt: `overwrite(o)/rename(r)/cancel(c)` (input `o/r/c`).

## Success closure

- After installer returns success, immediately end with:

```json
{"tool":"done","args":{}}
```

- Success response should contain install result only (id/name/path and related installer output).
- Forbidden after success:
  - re-running installer for same selection
  - additional unrelated shell commands
  - asking user for unrelated next tasks

## Frontmatter requirement

Installer uses strict validation:

- Source `SKILL.md` must be standard frontmatter format and include at least:
  - `name`
  - `description`

If source format is not compliant, installation must fail immediately.
Do not attempt automatic conversion in installer workflow.

## Failure discipline

- On any non-zero exit from installer script, output error summary and next actionable options only.
- On any non-zero exit from installer script, after one concise error summary, immediately end with `{"tool":"done","args":{}}`.
- If installer output contains `Installation aborted by user.` (for example user entered `n` at `Yes(y)/No(n)`, `c/C` at index selection, or `cancel(c)` in conflict resolution), treat it as final terminal state and immediately end with `{"tool":"done","args":{}}`.
- Forbidden after failure:
  - switching to unrelated workflows
  - asking unrelated inputs
  - repeated idle shell loops
