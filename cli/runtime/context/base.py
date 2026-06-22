"""Base class for a single model-context part.

Each concrete part renders one section of the system prompt. Parts are kept
deliberately thin: the actual text-building logic continues to live as
module-level ``build_*`` functions in ``prompt_composer`` (so they remain
individually unit-testable and patchable), and each part's :meth:`render`
delegates to its corresponding function. This abstraction makes the set of
context sections explicit, ordered, and independently extensible.
"""

from __future__ import annotations

from typing import Any


class ModelContextPart:
    """One section of the model-visible system prompt.

    Subclasses set :attr:`name` and :attr:`order` and implement :meth:`render`.
    ``order`` controls placement in the composed snapshot (ascending).
    """

    #: Stable identifier for the part (useful for diagnostics/tests).
    name: str = "part"

    #: Relative position in the composed system prompt (ascending order).
    order: int = 0

    #: When True, the part is only included if ``include_tools`` is set.
    requires_tools: bool = False

    def render(self, agent: Any, include_tools: bool) -> str:
        """Return this part's contribution to the system prompt.

        Implementations should return an empty string when the part has
        nothing to contribute for the current agent state.
        """
        raise NotImplementedError

    def should_include(self, include_tools: bool) -> bool:
        """Whether this part participates given the ``include_tools`` flag."""
        return (not self.requires_tools) or bool(include_tools)
