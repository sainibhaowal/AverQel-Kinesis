"""Runtime settings for the source-driven local API server."""

from collections.abc import Mapping

SOURCE_RELOAD_GRACEFUL_SHUTDOWN_SECONDS = 10


def backend_bind_host(environ: Mapping[str, str]) -> str:
    """Keep the embedded desktop API on loopback; preserve deployment binding elsewhere."""
    desktop_runtime = (
        environ.get("AVERQEL_DESKTOP_PACKAGED", "").strip() == "1"
        or environ.get("AVERQEL_DESKTOP_LOCAL_RUNTIME", "").strip() == "1"
    )
    return (
        "127.0.0.1"
        if desktop_runtime
        else "0.0.0.0"  # nosec B104 -- non-desktop deployments must accept remote clients.
    )


def graceful_shutdown_timeout(*, source_reload: bool) -> int | None:
    """Bound old-worker draining during development reloads only.

    Long-lived WebSocket sessions can otherwise keep Uvicorn's reloader waiting
    forever after a source change, leaving the listening port open without a
    responsive API. Packaged and non-reloading servers retain Uvicorn's default.
    """
    if source_reload:
        return SOURCE_RELOAD_GRACEFUL_SHUTDOWN_SECONDS
    return None
