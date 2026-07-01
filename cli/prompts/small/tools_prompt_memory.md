## Experiential Memory `memory_search` / `memory_add` / `memory_delete`

- Experiential memory stores internalized lessons, preferences, and conventions. Relevant snippets appear in the system message's experiential-memory block.
- Do not store code snippets, raw command output, or large logs.
- Call `memory_search` when the user asks about past context or when you need an identifier that may exist only in memory.
- `memory_add` stores short factual information: conventions, preferences, corrections. Do not store secrets. Do not use as substitute for `user_preferences_patch`.
- Use `memory_search` to find entries, then `memory_delete` when the user asks to forget or retract information.

