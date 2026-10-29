"""DeepSpace services.

The chat service re-export is resolved on first use, not while the package is
initialized. Modules in this package import each other during their own setup,
and one of those imports reaches a model module that reads
``session_log_crypto`` from this package. A package-level import of the chat
service would make that read load the service before the model module defines
its classes, which raises a circular-import error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .chat_service import DeepSpaceChatService

__all__ = ["DeepSpaceChatService"]


def __getattr__(name: str) -> Any:
    """Resolve the package's public service on first use.

    Args:
        name: The attribute the caller asked for.

    Returns:
        The requested service.

    Raises:
        AttributeError: When the package exports no such attribute.
    """
    if name == "DeepSpaceChatService":
        from .chat_service import DeepSpaceChatService

        return DeepSpaceChatService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
