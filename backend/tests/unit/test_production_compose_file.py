"""Tests for the production Compose file.

The checks cover the services the deployment needs, the commands the
containers run, the files the stack mounts, and the ports the start
scripts and the operations chapter already use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit_no_db

BACKEND_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = BACKEND_ROOT / "docker-compose.prod.yml"

#: The services the production stack is made of.
REQUIRED_SERVICES: tuple[str, ...] = (
    "api",
    "frontend",
    "worker",
    "scheduler",
    "postgres",
    "redis",
    "minio",
    "livekit",
    "caddy",
    "searxng",
    "clamav",
    "otel-collector",
)

#: The ports the start scripts and the operations chapter already use.
EXPECTED_PORTS: dict[str, tuple[str, ...]] = {
    "api": ("1000",),
    "frontend": ("1030",),
    "inference": ("1011",),
    "postgres": ("5432",),
    "redis": ("6379",),
    "minio": ("9000", "9001"),
    "searxng": ("8080",),
    "clamav": ("3310",),
    "livekit": ("7880", "7881/tcp", "7882/udp"),
    "otel-collector": ("4317", "4318"),
}


def _compose() -> dict[str, Any]:
    """Read the production Compose file.

    Returns:
        The parsed document.
    """
    document = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_the_compose_file_is_readable_yaml() -> None:
    document = _compose()
    assert "services" in document


def test_the_compose_file_defines_the_services_the_deployment_needs() -> None:
    services = _compose()["services"]
    for name in REQUIRED_SERVICES:
        assert name in services, f"the production stack is missing {name}"


def test_every_command_the_stack_runs_exists_in_the_repository() -> None:
    services = _compose()["services"]
    commands = [
        command
        for service in services.values()
        for command in service.get("command", [])
        if isinstance(command, str) and command.startswith("./")
    ]
    assert commands, "the stack runs no repository script"
    for command in commands:
        assert (BACKEND_ROOT / command[2:]).is_file(), f"{command} does not exist"


def test_every_file_the_stack_mounts_exists_in_the_repository() -> None:
    services = _compose()["services"]
    mounted: list[str] = []
    for service in services.values():
        for volume in service.get("volumes", []):
            if isinstance(volume, str) and volume.startswith("./"):
                mounted.append(volume.split(":", 1)[0])
    assert mounted
    for path in mounted:
        assert (BACKEND_ROOT / path[2:]).exists(), f"{path} does not exist"


def test_every_build_context_exists_in_the_repository() -> None:
    services = _compose()["services"]
    for name, service in services.items():
        build = service.get("build")
        if not build:
            continue
        context = BACKEND_ROOT / build["context"]
        assert context.exists(), f"the build context of {name} does not exist"


def test_the_stack_builds_the_images_the_release_script_builds() -> None:
    script = (BACKEND_ROOT / "scripts" / "release_build_images.sh").read_text(encoding="utf-8")
    services = _compose()["services"]
    for name, dockerfile in (("api", "Dockerfile"), ("worker", "Dockerfile.worker")):
        assert services[name]["build"]["dockerfile"] == dockerfile
        assert f'-f "$BACKEND_DIR/{dockerfile}"' in script


def test_the_services_publish_the_ports_the_scripts_and_the_chapter_use() -> None:
    services = _compose()["services"]
    for name, ports in EXPECTED_PORTS.items():
        exposed = {str(port) for port in services[name].get("expose", [])}
        for port in ports:
            assert port in exposed, f"{name} does not expose {port}"


def test_the_gateway_publishes_the_public_ports() -> None:
    services = _compose()["services"]
    published = " ".join(str(port) for port in services["caddy"].get("ports", []))
    assert ":80" in published
    assert ":443" in published


def test_the_api_and_the_workers_run_the_start_scripts() -> None:
    services = _compose()["services"]
    assert services["api"]["command"] == ["./scripts/start_api.sh"]
    assert services["worker"]["command"] == ["./scripts/start_worker.sh"]
    assert services["scheduler"]["command"] == ["./scripts/start_scheduler.sh"]
    assert services["inference"]["command"] == ["./scripts/start_inference.sh"]


def test_the_environment_file_is_optional_so_the_file_validates_on_its_own() -> None:
    services = _compose()["services"]
    for name in ("api", "worker", "scheduler", "inference"):
        env_files = services[name]["env_file"]
        assert isinstance(env_files, list)
        assert all(entry.get("required") is False for entry in env_files)


def test_the_scheduler_can_run_the_proactive_daemon() -> None:
    services = _compose()["services"]
    environment = services["scheduler"]["environment"]
    assert "AKS_DEEPSPACE_PROACTIVE_DAEMON_ENABLED" not in environment
