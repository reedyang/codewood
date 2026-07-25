import unittest
from types import SimpleNamespace

from cli.config.app_info import get_app_name
from cli.core import proposed_plan as pp
from cli.runtime import collaboration_mode as cm
from cli.tools.plan import UpdatePlanTool
from cli.tools.registry import (
    PLAN_MODE_EXCLUDED_TOOLS,
    PLAN_MODE_ONLY_TOOLS,
    iter_specs,
)


def _agent(plan_mode: bool) -> SimpleNamespace:
    return SimpleNamespace(
        _plan_mode_sticky=plan_mode,
        memory_enabled=True,
        subagents=[],
        _multimodal_enabled_for_current_model=lambda: True,
    )


class CollaborationModeLoaderTests(unittest.TestCase):
    def test_known_mode_names_includes_both_modes(self):
        names = cm.known_mode_names()
        self.assertIn("Agent", names)
        self.assertIn("Plan", names)

    def test_unknown_mode_falls_back_to_agent(self):
        self.assertEqual(cm.normalize_mode("nonsense"), cm.MODE_AGENT)
        self.assertEqual(cm.normalize_mode(""), cm.MODE_AGENT)

    def test_active_mode_tracks_sticky_flag(self):
        self.assertEqual(cm.active_mode_for_agent(_agent(True)), cm.MODE_PLAN)
        self.assertEqual(cm.active_mode_for_agent(_agent(False)), cm.MODE_AGENT)


class CollaborationModeGatingTests(unittest.TestCase):
    def test_request_user_input_visible_in_both_modes(self):
        plan_names = {
            s["function"]["name"] for s in iter_specs(_agent(plan_mode=True))
        }
        agent_names = {
            s["function"]["name"] for s in iter_specs(_agent(plan_mode=False))
        }
        self.assertIn("request_user_input", plan_names)
        self.assertIn("request_user_input", agent_names)

    def test_update_plan_visible_in_both_modes(self):
        plan_names = {
            s["function"]["name"] for s in iter_specs(_agent(plan_mode=True))
        }
        agent_names = {
            s["function"]["name"] for s in iter_specs(_agent(plan_mode=False))
        }
        self.assertIn("update_plan", plan_names)
        self.assertIn("update_plan", agent_names)

    def test_gating_sets_are_empty(self):
        self.assertEqual(PLAN_MODE_ONLY_TOOLS, frozenset())
        self.assertEqual(PLAN_MODE_EXCLUDED_TOOLS, frozenset())

    def test_update_plan_execute_rejected_in_plan_mode(self):
        result = UpdatePlanTool().execute(
            _agent(plan_mode=True),
            {"plan": [{"step": "do x", "status": "pending"}]},
        )
        self.assertFalse(result.get("success", True))
        self.assertIn("Plan mode", str(result.get("error", "")))


class ProposedPlanTests(unittest.TestCase):
    def test_extract_and_strip(self):
        text = "intro\n<proposed_plan>\n# Title\n- step\n</proposed_plan>\ntail"
        self.assertTrue(pp.has_proposed_plan(text))
        self.assertEqual(pp.latest_proposed_plan(text), "# Title\n- step")
        stripped = pp.strip_proposed_plan_blocks(text)
        self.assertNotIn("<proposed_plan>", stripped)
        self.assertIn("intro", stripped)
        self.assertIn("tail", stripped)

    def test_latest_wins_on_multiple_blocks(self):
        text = (
            "<proposed_plan>\nold\n</proposed_plan>\n"
            "<proposed_plan>\nnew\n</proposed_plan>"
        )
        self.assertEqual(pp.latest_proposed_plan(text), "new")

    def test_no_block_is_passthrough(self):
        self.assertFalse(pp.has_proposed_plan("just prose"))
        self.assertIsNone(pp.latest_proposed_plan("just prose"))
        self.assertEqual(pp.strip_proposed_plan_blocks("just prose"), "just prose")


class PlanChoiceLocaleTests(unittest.TestCase):
    def test_modify_label_interpolates_app_name(self):
        from cli.config.i18n import translate

        for lang in ("en", "zh-CN"):
            label = translate("runtime.plan_choice.modify", lang, app=get_app_name())
            self.assertIn(get_app_name(), label)
            self.assertNotIn("{app}", label)


if __name__ == "__main__":
    unittest.main()
