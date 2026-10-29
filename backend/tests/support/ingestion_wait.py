"""Helpers for tests that wait on the ingestion pipeline.

Ingestion runs on a worker thread, so a test that reads a document's status
right after the upload reads the status of a pipeline that is still running.
These helpers wait for the pipeline to reach a terminal state, so a test
asserts the outcome instead of the timing.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

#: The document states that mean the pipeline is still running.
ACTIVE_DOCUMENT_STATUSES = frozenset({"queued", "downloading", "parsing", "chunking", "embedding"})

#: How long a test waits for the pipeline before it gives up.
DEFAULT_INGESTION_TIMEOUT_SECONDS = 120.0

#: How long a test sleeps between two status reads.
POLL_INTERVAL_SECONDS = 0.2


def wait_for_document_status(
    client: TestClient,
    *,
    document_id: str,
    headers: dict[str, str],
    timeout_seconds: float = DEFAULT_INGESTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Wait for one document's ingestion to reach a terminal state.

    Args:
        client: The test client.
        document_id: The document to watch.
        headers: The request headers of the document's owner.
        timeout_seconds: How long to wait before giving up.

    Returns:
        The last status payload the server returned.

    Raises:
        AssertionError: When the pipeline is still running after the
            timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/documents/{document_id}/status", headers=headers)
        assert response.status_code == 200, response.text
        payload = dict(response.json())
        status = str(payload.get("status") or "")
        if status not in ACTIVE_DOCUMENT_STATUSES:
            return payload
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"the document stayed in {payload.get('status')!r} for {timeout_seconds} seconds: {payload}"
    )


def wait_for_document_indexed(
    client: TestClient,
    *,
    document_id: str,
    headers: dict[str, str],
    timeout_seconds: float = DEFAULT_INGESTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Wait for one document's ingestion to index it.

    Args:
        client: The test client.
        document_id: The document to watch.
        headers: The request headers of the document's owner.
        timeout_seconds: How long to wait before giving up.

    Returns:
        The status payload of the indexed document.

    Raises:
        AssertionError: When the pipeline did not index the document.
    """
    payload = wait_for_document_status(
        client,
        document_id=document_id,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    assert payload.get("status") == "indexed", payload
    return payload
