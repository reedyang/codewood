"""Model-context parts.

The system prompt sent to the model is assembled from several distinct
sections ("parts"). Each part is modeled as a small class deriving from
:class:`ModelContextPart` (see ``base.py``), with one class per file in this
package. ``prompt_composer.compose_system_prompt_snapshot`` iterates the
ordered registry exposed here to build the final snapshot.
"""

from .base import ModelContextPart
from .registry import ordered_context_parts

__all__ = ["ModelContextPart", "ordered_context_parts"]
