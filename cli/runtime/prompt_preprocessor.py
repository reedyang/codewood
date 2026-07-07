"""Prompt template preprocessor supporting conditional directives.

Supports the following syntax in prompt markdown files:

.. code-block:: text

    [[if $os="Windows"]]
    Content visible only on Windows
    [[else]]
    Content visible on other platforms
    [[endif]]

``[[else]]`` is optional. Directives can appear at any position in the text.
"""

from __future__ import annotations

import re
from typing import Dict

_PREPROC_PATTERN = re.compile(
    r"\[\[\s*if\s+\$(\w+)\s*=\s*\"([^\"]*)\"\s*\]\]"
    r"((?:(?!\[\[).)*)"
    r"(?:\[\[\s*else\s*\]\]((?:(?!\[\[).)*))?"
    r"\[\[\s*endif\s*\]\]",
    re.DOTALL,
)


def preprocess_prompt(text: str, variables: Dict[str, str]) -> str:
    """Process conditional directives in prompt template text.

    Parameters
    ----------
    text : str
        Raw prompt template text that may contain ``[[if $var="val"]]``,
        ``[[else]]``, and ``[[endif]]`` directives.
    variables : Dict[str, str]
        Variable values keyed by name (without the ``$`` prefix).
        For example ``{"os": "Windows"}``.

    Returns
    -------
    str
        Processed text with all conditional directives removed and branches
        resolved according to the supplied variable values.
    """
    if "[[if " not in text and "[[if\t" not in text:
        return text

    def _replace_match(m: re.Match) -> str:
        var_name = m.group(1)
        expected_value = m.group(2)
        true_content = m.group(3)
        false_content = m.group(4) or ""

        if variables.get(var_name) == expected_value:
            return true_content
        else:
            return false_content

    while "[[if " in text or "[[if\t" in text:
        text = _PREPROC_PATTERN.sub(_replace_match, text)

    return text
