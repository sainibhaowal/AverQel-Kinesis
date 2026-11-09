from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit_no_db

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_public_gateways_block_the_unauthenticated_prometheus_route() -> None:
    for filename in ("Caddyfile", "Caddyfile.localprod"):
        config = (BACKEND_ROOT / "ops" / "caddy" / filename).read_text()
        assert re.search(
            r"@metrics\s+path\s+/api/v1/metrics\s+handle @metrics\s+\{\s*respond 403\s*\}",
            config,
        )


def test_api_metrics_port_is_internal_in_production() -> None:
    production = (BACKEND_ROOT / "docker-compose.prod.yml").read_text()
    production_api = re.search(
        r"^  api:\n(?P<service>.*?)(?=^  frontend:)", production, flags=re.MULTILINE | re.DOTALL
    )
    assert production_api is not None
    assert '    expose:\n      - "1000"' in production_api.group("service")
    assert "    ports:" not in production_api.group("service")
