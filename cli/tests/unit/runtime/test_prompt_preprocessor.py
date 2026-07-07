import unittest

from cli.runtime.prompt_preprocessor import preprocess_prompt


class PreprocessPromptTests(unittest.TestCase):
    """Unit tests for the prompt template preprocessor."""

    def test_passthrough_when_no_directives(self):
        text = "plain content\nwithout any directives"
        result = preprocess_prompt(text, {"os": "Windows"})
        self.assertEqual(result, text)

    def test_passthrough_when_empty_string(self):
        self.assertEqual(preprocess_prompt("", {"os": "Windows"}), "")

    def test_if_else_true_branch(self):
        text = "before\n[[if $os=\"Windows\"]]\nwin\n[[else]]\nother\n[[endif]]\nafter"
        result = preprocess_prompt(text, {"os": "Windows"})
        self.assertEqual(result, "before\n\nwin\n\nafter")

    def test_if_else_false_branch(self):
        text = "before\n[[if $os=\"Windows\"]]\nwin\n[[else]]\nother\n[[endif]]\nafter"
        result = preprocess_prompt(text, {"os": "Linux"})
        self.assertEqual(result, "before\n\nother\n\nafter")

    def test_if_only_true_branch(self):
        text = "before\n[[if $os=\"Windows\"]]\nwin only\n[[endif]]\nafter"
        result = preprocess_prompt(text, {"os": "Windows"})
        self.assertEqual(result, "before\n\nwin only\n\nafter")

    def test_if_only_false_branch_removes_block(self):
        text = "before\n[[if $os=\"Windows\"]]\nwin only\n[[endif]]\nafter"
        result = preprocess_prompt(text, {"os": "Linux"})
        self.assertEqual(result, "before\n\nafter")

    def test_nested_if_both_true(self):
        text = """top
[[if $os="Windows"]]
win
[[if $editor="vscode"]]
vscode
[[endif]]
[[else]]
other os
[[endif]]
bottom"""
        result = preprocess_prompt(text, {"os": "Windows", "editor": "vscode"})
        self.assertIn("win", result)
        self.assertIn("vscode", result)
        self.assertNotIn("other os", result)

    def test_nested_if_outer_true_inner_false(self):
        text = """top
[[if $os="Windows"]]
win
[[if $editor="vscode"]]
vscode
[[endif]]
[[else]]
other os
[[endif]]
bottom"""
        result = preprocess_prompt(text, {"os": "Windows", "editor": "other"})
        self.assertIn("win", result)
        self.assertNotIn("vscode", result)
        self.assertNotIn("other os", result)

    def test_nested_if_outer_false(self):
        text = """top
[[if $os="Windows"]]
win
[[if $editor="vscode"]]
vscode
[[endif]]
[[else]]
other os
[[endif]]
bottom"""
        result = preprocess_prompt(text, {"os": "Linux"})
        self.assertNotIn("win", result)
        self.assertNotIn("vscode", result)
        self.assertIn("other os", result)

    def test_nested_if_with_else_in_inner(self):
        text = """top
[[if $os="Windows"]]
win
[[if $editor="vscode"]]
vscode
[[else]]
other editor
[[endif]]
[[else]]
not win
[[endif]]
bottom"""
        result = preprocess_prompt(text, {"os": "Windows", "editor": "vscode"})
        self.assertIn("win", result)
        self.assertIn("vscode", result)
        self.assertNotIn("other editor", result)
        self.assertNotIn("not win", result)

    def test_nested_if_with_else_in_inner_else_branch(self):
        text = """top
[[if $os="Windows"]]
win
[[if $editor="vscode"]]
vscode
[[else]]
other editor
[[endif]]
[[else]]
not win
[[endif]]
bottom"""
        result = preprocess_prompt(text, {"os": "Windows", "editor": "other"})
        self.assertIn("win", result)
        self.assertIn("other editor", result)
        self.assertNotIn("vscode", result)
        self.assertNotIn("not win", result)

    def test_inline_directives_true(self):
        text = "before[[if $os=\"Windows\"]]inline win[[else]]inline other[[endif]]after"
        result = preprocess_prompt(text, {"os": "Windows"})
        self.assertEqual(result, "beforeinline winafter")

    def test_inline_directives_false(self):
        text = "before[[if $os=\"Windows\"]]inline win[[else]]inline other[[endif]]after"
        result = preprocess_prompt(text, {"os": "Linux"})
        self.assertEqual(result, "beforeinline otherafter")

    def test_inline_if_only_true(self):
        text = "before[[if $os=\"Windows\"]]inline win[[endif]]after"
        result = preprocess_prompt(text, {"os": "Windows"})
        self.assertEqual(result, "beforeinline winafter")

    def test_inline_if_only_false(self):
        text = "before[[if $os=\"Windows\"]]inline win[[endif]]after"
        result = preprocess_prompt(text, {"os": "Linux"})
        self.assertEqual(result, "beforeafter")

    def test_multiple_if_blocks_both_true(self):
        text = """[[if $os="Windows"]]win[[endif]]
[[if $editor="vscode"]]vscode[[endif]]"""
        result = preprocess_prompt(text, {"os": "Windows", "editor": "vscode"})
        self.assertEqual(result, "win\nvscode")

    def test_multiple_if_blocks_one_false(self):
        text = """[[if $os="Windows"]]win[[endif]]
[[if $editor="vscode"]]vscode[[endif]]"""
        result = preprocess_prompt(text, {"os": "Windows", "editor": "other"})
        self.assertEqual(result, "win\n")

    def test_unknown_variable_treated_as_false(self):
        text = "[[if $editor=\"vscode\"]]vscode[[else]]other[[endif]]"
        result = preprocess_prompt(text, {"os": "Windows"})
        self.assertEqual(result, "other")

    def test_triple_nested_if(self):
        text = """a
[[if $x="1"]]
x1
[[if $y="2"]]
y2
[[if $z="3"]]
z3
[[endif]]
[[endif]]
[[endif]]
b"""
        result = preprocess_prompt(text, {"x": "1", "y": "2", "z": "3"})
        self.assertIn("x1", result)
        self.assertIn("y2", result)
        self.assertIn("z3", result)
        self.assertIn("a", result)
        self.assertIn("b", result)

    def test_triple_nested_mid_level_false(self):
        text = """a
[[if $x="1"]]
x1
[[if $y="2"]]
y2
[[if $z="3"]]
z3
[[endif]]
[[endif]]
[[endif]]
b"""
        result = preprocess_prompt(text, {"x": "1", "y": "wrong", "z": "3"})
        self.assertIn("x1", result)
        self.assertNotIn("y2", result)
        self.assertNotIn("z3", result)
        self.assertIn("a", result)
        self.assertIn("b", result)

    def test_whitespace_variations_in_directives(self):
        text = "[[if $os = \"Windows\"]]win[[else]]other[[endif]]"
        result = preprocess_prompt(text, {"os": "Windows"})
        self.assertEqual(result, "win")

    def test_extra_newlines_preserved(self):
        text = "a\n\n\n[[if $os=\"Windows\"]]\n\nwin\n\n[[endif]]\n\n\nb"
        result = preprocess_prompt(text, {"os": "Windows"})
        self.assertEqual(result, "a\n\n\n\n\nwin\n\n\n\n\nb")