---
name: clawhub-skill-installer
description: Search skills from ClawHub and install a selected skill into config_dir/skills with explicit CLI confirmation. Use whenever the user asks to browse/find/install skills from clawhub.ai, especially for "检索并安装 skill". Installation must stop on name conflicts with currently loaded skills unless an explicit conflict policy is provided.
license: Proprietary
---

# ClawHub Skill Installer (built-in)

## Purpose

- Search skill entries from `https://clawhub.ai/skills`
- Install selected skill into `<config_dir>/skills/<skill_id>/`
- Enforce explicit `--confirm YES` before installation
- Run fully non-interactively; Smart Shell no longer supports interactive script prompts
- Abort installation when conflict with currently loaded skill is detected unless explicitly resolved with `--on-conflict`

## CLI

Use bundled script:

```text
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" search --query "<keyword>" [--insecure|--no-verify]
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" install --detail-url "<skill detail url>" --config-dir "<config dir>" --confirm "YES" [--on-conflict abort|overwrite|rename] [--insecure|--no-verify]
```

## Workflow constraints

- `install` requires `--confirm YES`; otherwise script exits without writing files.
- For slash route `/clawhub-skill-installer <text>`, always treat `<text>` as the query.
- `install` must use `--detail-url`; do not install by rerunning search with a query/index.
- For an index selection, map the user's selected index to the `detail_url` printed by the immediately preceding `search` result for the same task.
- Do not reuse stale search results, unrelated task results, or guessed URLs.
- Confirmation must be supplied with `--confirm YES`.
- Conflict handling must be supplied with `--on-conflict`; if omitted, the installer uses `abort`.
- Do not ask for terminal input or rely on interactive prompts.
- Do not run placeholder shell commands (`echo waiting`, etc.).
- Task boundary is installation only; do not switch to unrelated tasks.
- Any shell call that runs `clawhub_installer.py` must be non-interactive.
- Forbidden for this skill: setting tool arg `interactive=true`.

## Tool-call contract (mandatory)

- For this skill, invoke installer commands only via:

```json
{"tool":"shell","args":{"command":"python \"<BUNDLE_ROOT>/scripts/clawhub_installer.py\" search --query \"<text>\"","interactive":false}}
```

- After the user selects an index from the search results, install using the exact `detail_url` from that result:

```json
{"tool":"shell","args":{"command":"python \"<BUNDLE_ROOT>/scripts/clawhub_installer.py\" install --detail-url \"<skill detail url>\" --config-dir \"<config dir>\" --confirm \"YES\" --on-conflict abort","interactive":false}}
```

- If user explicitly gives a detail URL, skip search and use the same install command.
- If a conflict abort occurs and the user asks to overwrite or rename, re-run the same install command with the same `--detail-url` and `--on-conflict overwrite` or `--on-conflict rename`.

## Mandatory routing for slash usage

When user input is slash-routed as:

- `/clawhub-skill-installer <text>`

the `<text>` part MUST be treated as the initial search query immediately.

Required behavior:

1. Do not ask for another keyword if `<text>` is non-empty.
2. If `<text>` itself is a ClawHub detail URL (e.g. `https://clawhub.ai/skills/...`), install this URL directly.
3. Otherwise run search non-interactively once:

```text
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" search --query "<text>"
```

4. Present the search result list to the user and ask for an index.
5. When the user replies with an index, find that numbered result in the immediately preceding search output and install by URL:

```text
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" install --detail-url "<detail_url from selected search result>" --config-dir "<config dir>" --confirm "YES" --on-conflict abort
```

6. Do not re-run search when the user chooses an index; search results may drift between calls.
7. If a config conflict aborts and the user chooses overwrite or rename, re-run install with the same selected `--detail-url` and the requested `--on-conflict` value.
8. Do not perform non-installer actions until install finishes or user cancels.

## Non-interactive controls

- Search result selection is model-side: map the user's selected index to the printed `detail_url`.
- Install confirmation: `--confirm YES`.
- Config conflict handling: `--on-conflict abort|overwrite|rename` (default `abort`).
- Builtin/workspace conflicts always abort.

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

## Frontmatter normalization

Installer writes standard skill frontmatter:

- Installed `SKILL.md` must include at least:
  - `name`
  - `description`

If the source `SKILL.md` is not standard frontmatter format or is missing required fields, the installer must continue installation and automatically rewrite the installed `SKILL.md` into standard frontmatter format. Preserve the original skill body as much as possible, infer `name` from existing frontmatter, heading, or URL slug, and infer `description` from existing frontmatter or the first content line.

If auto-normalization happens, installer output includes:

```text
normalized_frontmatter: yes
```

## Failure discipline

- On any non-zero exit from installer script, output error summary and next actionable options only.
- On any non-zero exit from installer script, after one concise error summary, immediately end with `{"tool":"done","args":{}}`.
- If installer output reports an aborted installation, treat it as final terminal state and immediately end with `{"tool":"done","args":{}}`.
- Forbidden after failure:
  - switching to unrelated workflows
  - asking unrelated inputs
  - repeated idle shell loops
