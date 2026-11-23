from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit_no_db

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_public_gateways_block_the_unauthenticated_prometheus_route() -> None:
    # The standalone Kinesis build ships no local-production gateway; the
    # remaining public gateway config must still refuse the metrics route.
    for filename in ("Caddyfile",):
        config = (BACKEND_ROOT / "ops" / "caddy" / filename).read_text()
        assert re.search(
            r"@metrics\s+path\s+/api/v1/metrics\s+handle @metrics\s+\{\s*respond 403\s*\}",
            config,
        )
