"""Branded types for DeepSpace Jobs service."""

from __future__ import annotations

from typing import TYPE_CHECKING, NewType

if TYPE_CHECKING:
    JobId = NewType("JobId", str)
    JobKind = NewType("JobKind", str)
    JobChannel = NewType("JobChannel", str)

from app.deepspace.utils.brand import branded

# Create branded types using the branded() function
JobId = branded("JobId", str)  # type: ignore[misc]
JobKind = branded("JobKind", str)  # type: ignore[misc]
JobChannel = branded("JobChannel", str)  # type: ignore[misc]


def job_id(value: str) -> JobId:
    return JobId(value)


def job_kind(value: str) -> JobKind:
    return JobKind(value)


def job_channel(value: str) -> JobChannel:
    return JobChannel(value)


__all__ = [
    "JobId",
    "job_id",
    "JobKind",
    "job_kind",
    "JobChannel",
    "job_channel",
]
