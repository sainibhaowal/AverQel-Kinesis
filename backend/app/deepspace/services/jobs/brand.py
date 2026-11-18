"""Branded types for DeepSpace Jobs service."""

from __future__ import annotations

from app.deepspace.utils.brand import branded

# Create branded types using the branded() function
JobId = branded("JobId", str)
JobKind = branded("JobKind", str)
JobChannel = branded("JobChannel", str)


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
