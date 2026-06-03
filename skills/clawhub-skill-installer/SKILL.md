---
name: clawhub-skill-installer
description: Search skills from ClawHub and install a selected skill with explicit CLI confirmation. Callers must provide an explicit install target via `--install-skills-root`. Use this whenever the user asks to browse, find, or install skills from clawhub.ai, especially for "search and install a skill". Installation must stop on name conflicts with currently loaded skills unless an explicit conflict policy is provided.
license: Proprietary
---

# ClawHub Skill Installer (built-in)

## Purpose

- Search skill entries from `https://clawhub.ai/skills`
- Install selected skill into `<install_skills_root>/<skill_id>/`
- Enforce explicit `--confirm YES` before installation
- Run fully non-interactively; interactive script prompts are not allowed
- Abort installation when conflict with currently loaded skill is detected unless explicitly resolved with `--on-conflict`

## CLI

Use bundled script:

```text
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" search --query "<keyword>" [--insecure|--no-verify]
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" install --detail-url "<skill detail url>" --install-skills-root "<skills root>" --confirm "YES" [--config-dir "<config dir for conflict scan>"] [--on-conflict abort|overwrite|rename] [--insecure|--no-verify]
```

## Workflow constraints

- `install` requires `--confirm YES`; otherwise script exits without writing files.
- Installation target rule:
  - `--install-skills-root` is mandatory for every install command.
  - If user explicitly specifies install location, pass `--install-skills-root "<that absolute path>"`.
  - If the user asks to install into the workspace, use the absolute path from the system prompt line `current workspace skills directory (absolute path)` as `--install-skills-root`.
  - If the user does not specify a location, use the system prompt line `default skills installation path (absolute path)` as `--install-skills-root`.
  - Never treat workspace as default install target. Only use workspace path when the user explicitly asks for workspace installation.
- For slash route `/clawhub-skill-installer <text>`, always treat `<text>` as the query.
- `install` must use `--detail-url`; do not install by rerunning search with a query/index.
- For an index selection, map the user's selected index to the `detail_url` printed by the immediately preceding `search` result for the same task.
- If search returns multiple candidates and the user did not explicitly authorize model-side selection (for example, "you choose"), ask the user to pick one candidate once by index or exact URL before running install. Do not auto-pick.
- For consecutive installations in one conversation, treat each new install request as a new selection round: do not reuse any previously chosen index, and ask again based on the current search result list.
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
{"tool":"shell","args":{"command":"python \"<BUNDLE_ROOT>/scripts/clawhub_installer.py\" install --detail-url \"<skill detail url>\" --install-skills-root \"<skills root>\" --confirm \"YES\" --on-conflict abort","interactive":false}}
```

- If the user explicitly gives a detail URL, skip search and use the same install command.
- If a conflict abort occurs and the user asks to overwrite or rename, rerun the same install command with the same `--detail-url` and `--on-conflict overwrite` or `--on-conflict rename`.

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
4.1 If there are multiple candidates and the user has not explicitly asked you to choose, do not decide on the user's behalf; require one explicit user selection before install.
5. When the user replies with an index, find that numbered result in the immediately preceding search output and install by URL:

```text
python "<BUNDLE_ROOT>/scripts/clawhub_installer.py" install --detail-url "<detail_url from selected search result>" --install-skills-root "<skills root>" --confirm "YES" --on-conflict abort
```

6. Do not re-run search when the user chooses an index; search results may drift between calls.
6.1 For a later new install request, do not inherit or reuse any prior index selection from earlier installs; present current results and require a fresh user choice.
7. If a config conflict aborts and the user chooses overwrite or rename, re-run install with the same selected `--detail-url` and the requested `--on-conflict` value.
8. Do not perform non-installer actions until install finishes or user cancels.

## Non-interactive controls

- Search result selection is model-side mapping only: after user explicitly selects an index or URL, map that selection to the printed `detail_url`; do not auto-choose from multiple candidates unless user explicitly delegates selection.
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

- On any non-zero exit from the installer script, output only an error summary and the next actionable options.
- On any non-zero exit from installer script, after one concise error summary, immediately end with `{"tool":"done","args":{}}`.
- If installer output reports an aborted installation, treat it as a final terminal state and immediately end with `{"tool":"done","args":{}}`.
- Forbidden after failure:
  - switching to unrelated workflows
  - asking unrelated inputs
  - repeated idle shell loops
