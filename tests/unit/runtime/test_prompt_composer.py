import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.runtime import prompt_composer


class PromptComposerTests(unittest.TestCase):
    def _make_agent(
        self,
        *,
        config_dir: Path,
        workspace_root: Path,
        work_directory: Path | None = None,
        workspace_config_dir: Path | None = None,
    ) -> SimpleNamespace:
        workspace_dir = work_directory or workspace_root
        return SimpleNamespace(
            config_dir=config_dir,
            workspace_root=workspace_root,
            work_directory=workspace_dir,
            workspace_config_dir=workspace_config_dir or (workspace_root / ".smartshell"),
            _base_system_prompt="BASE",
            tool_specs=[],
            tools_prompt_template="",
            _project_context_tool_allowed=lambda: True,
            _use_standard_openai_tools_call=lambda: True,
        )

    def test_prompt_composer_no_longer_exposes_file_ops_or_rg_helpers(self):
        """The OS-specific file-operation prompt append and the entire
        ripgrep-detection chain that powered it have been removed.
        Guard against accidental reintroduction by asserting none of
        those symbols (including the legacy anchor substitution
        helpers) are exposed on the module surface."""
        for removed_attr in (
            "render_workspace_prompt_variables",
            "_rg_prompt_variables",
            "build_os_file_ops_prompt_append",
            "has_usable_workspace_rg_bin",
            "_usable_workspace_rg_bin_path",
            "_rg_bin_candidates",
            "_is_usable_rg_executable",
            "_workspace_bin_dir",
            "_TOOLS_FILE_SEARCH_NEARBY_RULE_FALLBACK",
            "_TOOLS_FILE_SEARCH_NEARBY_RULE_RG_ONLY",
        ):
            self.assertFalse(
                hasattr(prompt_composer, removed_attr),
                f"{removed_attr!r} should have been removed from prompt_composer",
            )

    def test_standard_tools_prompt_strips_simulated_json_examples(self):
        agent = SimpleNamespace(
            tools_prompt_template=prompt_composer.load_tools_prompt_template(),
            tools_prompt_mcp_management_template=(
                prompt_composer.load_tools_prompt_mcp_management_template()
            ),
            tools_prompt_memory_template=(
                prompt_composer.load_tools_prompt_memory_template()
            ),
            tool_specs=[
                {
                    "function": {
                        "name": "shell",
                        "description": "Run command",
                        "parameters": {"properties": {"command": {}}},
                    }
                },
                {
                    "function": {
                        "name": "mcp_server_info",
                        "description": "Show MCP server details",
                        "parameters": {"properties": {"server": {}}},
                    }
                }
            ],
            _project_context_tool_allowed=lambda: True,
            _use_standard_openai_tools_call=lambda: True,
            mcp_tools_enabled=False,
            memory_enabled=False,
        )

        text = prompt_composer.build_tools_prompt_append(agent)

        self.assertIn("Tool Call Mode: Standard API tool_calls", text)
        self.assertIn("reply in natural language with no tool_calls", text)
        self.assertIn("Visible text may contain only user-visible", text)
        self.assertIn("Never print any tool-call representation", text)
        self.assertIn("Available tools:", text)
        self.assertIn("For software development tasks", text)
        self.assertIn("project_context_search", text)
        self.assertNotIn("- mcp_server_info:", text)
        self.assertNotIn('{"tool"', text)
        self.assertNotIn("```json", text)
        # When MCP-management tools are gated off, the optional side prompt must not be injected.
        self.assertNotIn("MCP Tool Selection Boundaries", text)
        self.assertNotIn("mcp_server_info", text)
        # When memory tools are gated off, the experiential-memory section must not be injected.
        self.assertNotIn("Experiential Memory", text)
        self.assertNotIn("memory_search", text)

        agent.mcp_tools_enabled = True
        visible_text = prompt_composer.build_tools_prompt_append(agent)
        self.assertIn("- mcp_server_info:", visible_text)
        # When MCP management is enabled, the side template content is appended.
        self.assertIn("MCP Tool Selection Boundaries", visible_text)
        self.assertIn("mcp_server_info", visible_text)

    def test_tools_prompt_omits_mcp_management_section_when_template_missing(self):
        agent = SimpleNamespace(
            tools_prompt_template="## Tools",
            tools_prompt_mcp_management_template="",
            tools_prompt_memory_template="",
            tool_specs=[],
            _project_context_tool_allowed=lambda: False,
            _use_standard_openai_tools_call=lambda: True,
            mcp_tools_enabled=True,
            memory_enabled=True,
        )
        text = prompt_composer.build_tools_prompt_append(agent)
        self.assertNotIn("MCP Tool Selection Boundaries", text)
        self.assertNotIn("Experiential Memory", text)

    def test_load_tools_prompt_mcp_management_template_returns_section_text(self):
        text = prompt_composer.load_tools_prompt_mcp_management_template()
        self.assertIn("MCP Tool Selection Boundaries", text)
        self.assertIn("mcp_server_info", text)

    def test_load_tools_prompt_memory_template_returns_section_text(self):
        text = prompt_composer.load_tools_prompt_memory_template()
        self.assertIn("Experiential Memory", text)
        self.assertIn("memory_search", text)
        self.assertIn("memory_add", text)
        self.assertIn("memory_delete", text)

    def test_tools_prompt_injects_memory_section_when_memory_enabled(self):
        agent = SimpleNamespace(
            tools_prompt_template="## Tools",
            tools_prompt_mcp_management_template="",
            tools_prompt_memory_template=(
                prompt_composer.load_tools_prompt_memory_template()
            ),
            tool_specs=[
                {
                    "function": {
                        "name": "memory_search",
                        "description": "Search experiential memory",
                        "parameters": {"properties": {"query": {}}},
                    }
                },
                {
                    "function": {
                        "name": "memory_add",
                        "description": "Add a memory entry",
                        "parameters": {"properties": {"content": {}}},
                    }
                },
            ],
            _project_context_tool_allowed=lambda: False,
            _use_standard_openai_tools_call=lambda: True,
            mcp_tools_enabled=False,
            memory_enabled=True,
        )

        text_enabled = prompt_composer.build_tools_prompt_append(agent)
        self.assertIn("Experiential Memory", text_enabled)
        self.assertIn("- memory_search:", text_enabled)
        self.assertIn("- memory_add:", text_enabled)

        agent.memory_enabled = False
        text_disabled = prompt_composer.build_tools_prompt_append(agent)
        # Side prompt suppressed and memory tool catalog entries filtered.
        self.assertNotIn("Experiential Memory", text_disabled)
        self.assertNotIn("- memory_search:", text_disabled)
        self.assertNotIn("- memory_add:", text_disabled)

    def test_tools_prompt_omits_memory_section_when_template_missing(self):
        agent = SimpleNamespace(
            tools_prompt_template="## Tools",
            tools_prompt_mcp_management_template="",
            tools_prompt_memory_template="",
            tool_specs=[],
            _project_context_tool_allowed=lambda: False,
            _use_standard_openai_tools_call=lambda: True,
            mcp_tools_enabled=False,
            memory_enabled=True,
        )
        text = prompt_composer.build_tools_prompt_append(agent)
        self.assertNotIn("Experiential Memory", text)

    def test_load_tools_spec_filters_mcp_management_tools_when_disabled(self):
        agent = SimpleNamespace(mcp_tools_enabled=False)
        specs_disabled = prompt_composer.load_tools_spec_from_jsonc(agent)
        names_disabled = {
            str(((s or {}).get("function", {}) or {}).get("name", "")).strip()
            for s in specs_disabled
        }
        for gated in (
            "mcp_server_info",
            "mcp_disable_tools",
            "mcp_enable_tools",
            "mcp_list_disabled_tools",
            "mcp_sampling_create_message",
            "mcp_completion_complete",
        ):
            self.assertNotIn(gated, names_disabled)
        self.assertIn("mcp_status", names_disabled)
        self.assertIn("mcp_call_tool", names_disabled)

        agent_enabled = SimpleNamespace(mcp_tools_enabled=True)
        specs_enabled = prompt_composer.load_tools_spec_from_jsonc(agent_enabled)
        names_enabled = {
            str(((s or {}).get("function", {}) or {}).get("name", "")).strip()
            for s in specs_enabled
        }
        self.assertIn("mcp_server_info", names_enabled)

    def test_build_mcp_system_append_includes_initialize_instructions(self):
        class _FakeMcpManager:
            def get_status(self):
                return {"servers": {"codegraph": {"state": "success"}}}

            def cached_initialize_instructions_for_prompt(self):
                return "- codegraph:\n  CodeGraph instructions\n  Use codegraph_explore first"

            def cached_tools_for_prompt(self):
                return "No cached MCP tools yet (run mcp_list_tools first)."

            def cached_resources_for_prompt(self):
                return "No cached MCP resources yet (run mcp_list_resources first)."

            def cached_prompts_for_prompt(self):
                return "No cached MCP prompts yet (run mcp_list_prompts first)."

        agent = self._make_agent(config_dir=Path("D:/config"), workspace_root=Path("D:/workspace"))
        agent.mcp_config = {
            "mcpServers": {
                "codegraph": {
                    "type": "stdio",
                    "command": "codegraph",
                    "args": ["serve", "--mcp"],
                }
            }
        }
        agent.mcp_manager = _FakeMcpManager()

        text = prompt_composer.build_mcp_system_append(agent)

        self.assertIn("MCP initialize instructions from connected servers", text)
        self.assertIn("active guidance", text)
        self.assertIn("follow its instructions", text)
        self.assertIn("codegraph", text)
        self.assertIn("CodeGraph instructions", text)
        self.assertIn("Use codegraph_explore first", text)

    def test_standard_skill_section_hint_uses_standard_tools_not_json(self):
        text, meta = prompt_composer.render_skill_section_payload(
            sections=["part 1", "part 2"],
            requested_section=1,
            full=False,
            initial_sections=1,
        )

        self.assertFalse(meta.get("full"))
        self.assertIn("standard tools", text)
        self.assertIn("section=2", text)
        self.assertNotIn('{"tool"', text)
        self.assertNotIn("```json", text)

    def test_build_agents_md_system_append_prefers_override_files_in_config_and_project_chain(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            repo_root = root / "repo"
            workspace_root = repo_root / "backend" / "api"
            config_dir.mkdir()
            workspace_root.mkdir(parents=True)
            (repo_root / ".git").mkdir()

            (config_dir / "AGENTS.md").write_text("config-base", encoding="utf-8")
            (config_dir / "AGENTS.override.md").write_text("config-override", encoding="utf-8")
            (repo_root / "AGENTS.md").write_text("repo-base", encoding="utf-8")
            (repo_root / "backend" / "AGENTS.md").write_text("backend-base", encoding="utf-8")
            (repo_root / "backend" / "AGENTS.override.md").write_text("backend-override", encoding="utf-8")
            (workspace_root / "AGENTS.md").write_text("api-base", encoding="utf-8")

            agent = self._make_agent(config_dir=config_dir, workspace_root=workspace_root)

            text = prompt_composer.build_agents_md_system_append(agent)

            self.assertIn("config-override", text)
            self.assertNotIn("config-base", text)
            self.assertIn("repo-base", text)
            self.assertIn("backend-override", text)
            self.assertNotIn("backend-base", text)
            self.assertIn("api-base", text)
            self.assertLess(text.index("config-override"), text.index("repo-base"))
            self.assertLess(text.index("repo-base"), text.index("backend-override"))
            self.assertLess(text.index("backend-override"), text.index("api-base"))

    def test_build_agents_md_system_append_uses_current_workspace_only_when_not_in_git_repo(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            workspace_root = root / "plain" / "workspace"
            config_dir.mkdir()
            workspace_root.mkdir(parents=True)

            (config_dir / "AGENTS.md").write_text("config-base", encoding="utf-8")
            (root / "plain" / "AGENTS.md").write_text("parent-should-not-load", encoding="utf-8")
            (workspace_root / "AGENTS.md").write_text("workspace-only", encoding="utf-8")

            agent = self._make_agent(config_dir=config_dir, workspace_root=workspace_root)

            text = prompt_composer.build_agents_md_system_append(agent)

            self.assertIn("config-base", text)
            self.assertIn("workspace-only", text)
            self.assertNotIn("parent-should-not-load", text)

    def test_build_agents_md_system_append_does_not_load_workspace_config_dir_agents(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            repo_root = root / "repo"
            workspace_root = repo_root / "project"
            workspace_config_dir = workspace_root / ".smartshell"
            config_dir.mkdir()
            workspace_config_dir.mkdir(parents=True)
            (repo_root / ".git").mkdir()

            (config_dir / "AGENTS.md").write_text("config-base", encoding="utf-8")
            (workspace_root / "AGENTS.md").write_text("project-base", encoding="utf-8")
            (workspace_config_dir / "AGENTS.md").write_text("workspace-config-should-not-load", encoding="utf-8")

            agent = self._make_agent(
                config_dir=config_dir,
                workspace_root=workspace_root,
                workspace_config_dir=workspace_config_dir,
            )

            text = prompt_composer.build_agents_md_system_append(agent)

            self.assertIn("config-base", text)
            self.assertIn("project-base", text)
            self.assertNotIn("workspace-config-should-not-load", text)

    def test_build_agents_md_system_append_refreshes_cache_when_override_file_appears(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            workspace_root = root / "workspace"
            config_dir.mkdir()
            workspace_root.mkdir()

            target = workspace_root / "AGENTS.md"
            target.write_text("base-workspace", encoding="utf-8")
            agent = self._make_agent(config_dir=config_dir, workspace_root=workspace_root)

            first = prompt_composer.build_agents_md_system_append(agent)
            self.assertIn("base-workspace", first)

            (workspace_root / "AGENTS.override.md").write_text("override-workspace", encoding="utf-8")
            second = prompt_composer.build_agents_md_system_append(agent)

            self.assertIn("override-workspace", second)
            self.assertNotIn("base-workspace", second)

    def test_build_agents_md_system_append_enforces_total_size_limit(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            repo_root = root / "repo"
            workspace_root = repo_root / "deep"
            config_dir.mkdir()
            workspace_root.mkdir(parents=True)
            (repo_root / ".git").mkdir()

            huge = "A" * (33 * 1024)
            (config_dir / "AGENTS.md").write_text(huge, encoding="utf-8")
            (repo_root / "AGENTS.md").write_text("repo-should-be-truncated", encoding="utf-8")

            agent = self._make_agent(config_dir=config_dir, workspace_root=workspace_root)
            text = prompt_composer.build_agents_md_system_append(agent)

            self.assertLessEqual(len(text.encode("utf-8")), 32 * 1024)
            self.assertNotIn("repo-should-be-truncated", text)

    def test_compose_system_prompt_snapshot_picks_up_agents_file_changes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            workspace_root = root / "workspace"
            config_dir.mkdir()
            workspace_root.mkdir()

            agents_file = config_dir / "AGENTS.md"
            agents_file.write_text("alpha", encoding="utf-8")
            agent = self._make_agent(config_dir=config_dir, workspace_root=workspace_root)

            with patch("src.runtime.prompt_composer.build_user_preferences_system_append", return_value=""), patch(
                "src.runtime.prompt_composer.build_mcp_system_append", return_value=""
            ), patch("src.runtime.prompt_composer.build_runtime_cache_prompt_append", return_value=""):
                first = prompt_composer.compose_system_prompt_snapshot(agent, include_tools=False)
                agents_file.write_text("beta-different-size", encoding="utf-8")
                second = prompt_composer.compose_system_prompt_snapshot(agent, include_tools=False)

            self.assertIn("alpha", first)
            self.assertIn("beta-different-size", second)
            self.assertNotIn("alpha", second)


if __name__ == "__main__":
    unittest.main()
