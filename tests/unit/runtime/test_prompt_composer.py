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

    def test_has_usable_workspace_rg_bin_true_when_candidate_is_usable(self):
        candidate = Path("D:/repo/bin/rg.exe")
        with patch("src.runtime.prompt_composer._rg_bin_candidates", return_value=[candidate]):
            with patch(
                "src.runtime.prompt_composer._is_usable_rg_executable",
                side_effect=lambda p: p == candidate,
            ):
                self.assertTrue(prompt_composer.has_usable_workspace_rg_bin())

    def test_has_usable_workspace_rg_bin_false_when_all_candidates_unusable(self):
        candidates = [Path("D:/repo/bin/rg.exe"), Path("D:/repo/bin/rg.cmd")]
        with patch("src.runtime.prompt_composer._rg_bin_candidates", return_value=candidates):
            with patch("src.runtime.prompt_composer._is_usable_rg_executable", return_value=False):
                self.assertFalse(prompt_composer.has_usable_workspace_rg_bin())

    def test_render_workspace_prompt_variables_uses_rg_rules_when_rg_available(self):
        original = (
            "A\n"
            "{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}\n"
            "{{TOOLS_FILE_SEARCH_NEARBY_RULE}}\n"
            "B"
        )
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=True):
            updated = prompt_composer.render_workspace_prompt_variables(original)
        self.assertIn("first run `rg`", updated)
        self.assertIn("first use `rg` to locate matches", updated)
        self.assertNotIn("{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}", updated)
        self.assertNotIn("{{TOOLS_FILE_SEARCH_NEARBY_RULE}}", updated)

    def test_render_workspace_prompt_variables_uses_fallback_rules_when_rg_unavailable(self):
        original = (
            "{{SYSTEM_FILE_SEARCH_NEARBY_RULE}}\n{{TOOLS_FILE_SEARCH_NEARBY_RULE}}"
        )
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=False):
            updated = prompt_composer.render_workspace_prompt_variables(original)
        self.assertIn("first run a search such as `Select-String` or `rg`", updated)
        self.assertIn("first locate matches", updated)

    def test_build_os_file_ops_prompt_append_uses_rg_search_line_when_available(self):
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=True):
            text = prompt_composer.build_os_file_ops_prompt_append()
        self.assertIn("first use `rg` to locate matches", text)

    def test_build_os_file_ops_prompt_append_uses_generic_search_line_when_rg_unavailable(self):
        with patch("src.runtime.prompt_composer.has_usable_workspace_rg_bin", return_value=False):
            text = prompt_composer.build_os_file_ops_prompt_append()
        self.assertIn("first locate matches", text)
        self.assertNotIn("first use `rg` to locate matches", text)

    def test_standard_tools_prompt_strips_simulated_json_examples(self):
        agent = SimpleNamespace(
            tools_prompt_template=prompt_composer.load_tools_prompt_template(),
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
        )

        text = prompt_composer.build_tools_prompt_append(agent)

        self.assertIn("Tool Call Mode: Standard API tool_calls", text)
        self.assertIn("HARD REQUIREMENT", text)
        self.assertIn("content-only assistant message is invalid", text)
        self.assertIn("call `done` through standard API `tool_calls`", text)
        self.assertIn("Visible text may contain only user-visible", text)
        self.assertIn("Never print any tool-call representation", text)
        self.assertIn("Available tools:", text)
        self.assertIn("For software development tasks", text)
        self.assertIn("project_context_search", text)
        self.assertNotIn("- mcp_server_info:", text)
        self.assertNotIn('{"tool"', text)
        self.assertNotIn("```json", text)

        agent.mcp_tools_enabled = True
        visible_text = prompt_composer.build_tools_prompt_append(agent)
        self.assertIn("- mcp_server_info:", visible_text)

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
            ), patch("src.runtime.prompt_composer.build_runtime_cache_prompt_append", return_value=""), patch(
                "src.runtime.prompt_composer.build_os_file_ops_prompt_append", return_value=""
            ):
                first = prompt_composer.compose_system_prompt_snapshot(agent, include_tools=False)
                agents_file.write_text("beta-different-size", encoding="utf-8")
                second = prompt_composer.compose_system_prompt_snapshot(agent, include_tools=False)

            self.assertIn("alpha", first)
            self.assertIn("beta-different-size", second)
            self.assertNotIn("alpha", second)


if __name__ == "__main__":
    unittest.main()
