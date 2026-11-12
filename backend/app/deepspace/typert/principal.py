"""The identity of a capability caller.

DeepSpace runs one owning user per local installation, but a capability can
still be called by a plugin, a child agent, or a scheduled worker.  A
principal names that caller without changing the authenticated tenant/user
pair on the request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

PrincipalKind = Literal["owner", "user", "plugin", "subagent", "service"]


@dataclass(frozen=True, slots=True)
class Principal:
    """One caller identity attached to a capability invocation.

    Attributes:
        kind: ``owner`` for the product itself, otherwise the caller kind.
        id: A stable caller id, such as a plugin id or a child run id.
        tenant_id: The tenant the caller acts in.
        display_name: An optional label for diagnostics.
    """

    kind: PrincipalKind
    id: str
    tenant_id: uuid.UUID | None = None
    display_name: str | None = None

    @property
    def is_owner(self) -> bool:
        """Whether this is the owning product caller."""
        return self.kind == "owner"


def principal_of(invocation: object | None) -> Principal | None:
    """Return the non-owner caller of an invocation.

    Args:
        invocation: The current :class:`~app.deepspace.typert.invocation.RemoteInvocation`,
            or ``None`` outside a capability call.

    Returns:
        The caller's principal when it is not the owner; ``None`` for the
        owner and for trusted in-process code outside a call.
    """
    if invocation is None:
        return None
    principal = getattr(invocation, "principal", None)
    if principal is None or principal.kind == "owner":
        return None
    return principal


__all__ = ["Principal", "PrincipalKind", "principal_of"]
