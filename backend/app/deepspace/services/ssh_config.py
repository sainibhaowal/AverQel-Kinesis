"""Validated, versioned SSH helper launch and protocol settings."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _remote_absolute_path(value: str) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or "\x00" in path or any(ord(char) < 32 for char in path):
        raise ValueError(
            "Remote helper paths must be absolute POSIX paths without control characters."
        )
    if any(part in {".", ".."} for part in PurePosixPath(path).parts):
        raise ValueError("Remote helper paths cannot contain dot segments.")
    return path


class _LaunchBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeScriptLaunch(_LaunchBase):
    """Launch a pinned JavaScript helper using Node."""

    kind: Literal["node-script"]
    node_path: str = "node"
    bootstrap_path: str
    bootstrap_hash: str

    @model_validator(mode="after")
    def validate_launch(self) -> NodeScriptLaunch:
        if self.node_path.startswith("/"):
            _remote_absolute_path(self.node_path)
        if not self.node_path or "\x00" in self.node_path:
            raise ValueError("The Node executable name is invalid.")
        _remote_absolute_path(self.bootstrap_path)
        if not _SHA256.fullmatch(self.bootstrap_hash):
            raise ValueError("The node-script bootstrap hash must be a lowercase SHA-256 digest.")
        return self


class ExecutableLaunch(_LaunchBase):
    """Launch a pinned native helper executable."""

    kind: Literal["executable"]
    executable_path: str
    executable_hash: str

    @model_validator(mode="after")
    def validate_launch(self) -> ExecutableLaunch:
        _remote_absolute_path(self.executable_path)
        if not _SHA256.fullmatch(self.executable_hash):
            raise ValueError("The executable helper hash must be a lowercase SHA-256 digest.")
        return self


HelperLaunch = Annotated[NodeScriptLaunch | ExecutableLaunch, Field(discriminator="kind")]
_LAUNCH_ADAPTER: TypeAdapter[HelperLaunch] = TypeAdapter(HelperLaunch)


class SshRuntimeConfig(BaseModel):
    """Per-host helper contract and bounded RPC policy stored in metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    launch: HelperLaunch | None = None
    helper_hash: str | None = None
    workspace: str | None = None
    sandbox_mode: Literal["bubblewrap", "disabled"] = "bubblewrap"
    network_access: bool = False
    request_timeout_ms: int = Field(default=30_000, ge=100, le=3_600_000)
    max_frame_bytes: int = Field(default=2_000_000, ge=1_024, le=8_000_000)
    max_pending: int = Field(default=16, ge=1, le=128)
    lease_ms: int = Field(default=90_000, ge=1_000, le=86_400_000)
    heartbeat_ms: int = Field(default=15_000, ge=250, le=43_200_000)

    @model_validator(mode="before")
    @classmethod
    def parse_discriminated_launch(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        launch = value.get("launch")
        if launch is not None:
            value = dict(value)
            value["launch"] = _LAUNCH_ADAPTER.validate_python(launch)
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> SshRuntimeConfig:
        if self.heartbeat_ms * 2 > self.lease_ms:
            raise ValueError("The heartbeat interval must be no more than half the SSH lease.")
        if self.helper_hash is not None and not _SHA256.fullmatch(self.helper_hash):
            raise ValueError("The helper hash must be a lowercase SHA-256 digest.")
        launch_hash = (
            self.launch.bootstrap_hash
            if isinstance(self.launch, NodeScriptLaunch)
            else self.launch.executable_hash if isinstance(self.launch, ExecutableLaunch) else None
        )
        if (
            self.helper_hash is not None
            and launch_hash is not None
            and self.helper_hash != launch_hash
        ):
            raise ValueError("The helper hash must match the selected launch's pinned hash.")
        if self.workspace is not None:
            _remote_absolute_path(self.workspace)
        return self


def parse_ssh_runtime_config(value: object) -> SshRuntimeConfig:
    """Validate untrusted host metadata as the versioned runtime contract."""
    if value is None:
        return SshRuntimeConfig()
    if isinstance(value, SshRuntimeConfig):
        return value
    return SshRuntimeConfig.model_validate(value)


__all__ = [
    "ExecutableLaunch",
    "HelperLaunch",
    "NodeScriptLaunch",
    "SshRuntimeConfig",
    "parse_ssh_runtime_config",
]
