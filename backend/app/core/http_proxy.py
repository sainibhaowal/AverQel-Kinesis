"""Shared, validated proxy policy for trusted outbound HTTP clients."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.launch_environment import LaunchEnvironment, get_launch_environment

_PROXY_NAMES = {
    "http": ("http_proxy", "HTTP_PROXY"),
    "https": ("https_proxy", "HTTPS_PROXY"),
    "all": ("all_proxy", "ALL_PROXY"),
}
_NO_PROXY_NAMES = ("no_proxy", "NO_PROXY")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _valid_proxy_url(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    return raw.rstrip("/")


def _host_port(host: str, port: int | None) -> tuple[str, int | None]:
    normalized = host.strip().strip("[]").rstrip(".").casefold()
    return normalized, port


def _is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _parse_no_proxy(raw: str | None) -> tuple[str, ...]:
    entries = [part.strip() for part in (raw or "").split(",") if part.strip()]
    entries.extend(_LOOPBACK)
    return tuple(dict.fromkeys(entries))


def _bypasses(host: str, port: int | None, entries: tuple[str, ...]) -> bool:
    target_host, target_port = _host_port(host, port)
    if _is_loopback(target_host):
        return True
    try:
        target_ip = ipaddress.ip_address(target_host)
    except ValueError:
        target_ip = None
    for raw_entry in entries:
        entry = raw_entry.strip().casefold()
        if entry == "*":
            return True
        if "/" in entry and target_ip is not None:
            try:
                if target_ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        if entry.startswith("[") and "]" in entry:
            closing = entry.index("]")
            entry_host = entry[1:closing]
            port_text = entry[closing + 1 :]
            entry_port_text = port_text[1:] if port_text.startswith(":") else ""
        elif entry.count(":") == 1:
            entry_host, _, entry_port_text = entry.rpartition(":")
        else:
            entry_host, entry_port_text = entry, ""
        try:
            entry_port = int(entry_port_text) if entry_port_text else None
        except ValueError:
            continue
        if entry_port is not None and entry_port != target_port:
            continue
        entry_host = entry_host.removeprefix("*.").removeprefix(".").strip("[]")
        if target_host == entry_host or target_host.endswith(f".{entry_host}"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ProxyPolicy:
    """Resolved proxy configuration with no secret-bearing diagnostics."""

    http_proxy: str | None
    https_proxy: str | None
    no_proxy: tuple[str, ...]
    invalid_variables: tuple[str, ...] = ()

    def proxy_for_url(self, url: str) -> str | None:
        """Return the applicable validated proxy, or ``None`` for direct access."""
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if _bypasses(parsed.hostname, port, self.no_proxy):
            return None
        return self.https_proxy if parsed.scheme == "https" else self.http_proxy

    def child_environment(self, base: dict[str, str]) -> dict[str, str]:
        """Return a fresh child environment containing only normalized policy keys."""
        result = dict(base)
        for name in (
            *_PROXY_NAMES["http"],
            *_PROXY_NAMES["https"],
            *_PROXY_NAMES["all"],
            *_NO_PROXY_NAMES,
        ):
            result.pop(name, None)
        if self.http_proxy:
            result["HTTP_PROXY"] = self.http_proxy
        if self.https_proxy:
            result["HTTPS_PROXY"] = self.https_proxy
        if self.no_proxy:
            result["NO_PROXY"] = ",".join(self.no_proxy)
        return result

    def diagnostics(self) -> dict[str, object]:
        """Expose presence and invalid variable names, never proxy credentials/URLs."""
        return {
            "http_proxy_configured": self.http_proxy is not None,
            "https_proxy_configured": self.https_proxy is not None,
            "no_proxy_entry_count": len(self.no_proxy),
            "invalid_variables": list(self.invalid_variables),
        }


def resolve_proxy_policy(environment: LaunchEnvironment | None = None) -> ProxyPolicy:
    """Resolve scheme-specific proxy variables, falling back to ALL_PROXY."""
    snapshot = environment or get_launch_environment()
    invalid: list[str] = []

    def first(names: tuple[str, ...]) -> str | None:
        for name in names:
            value = snapshot.get(name)
            if value is not None and value.strip():
                return value.strip()
        return None

    all_proxy_raw = first(_PROXY_NAMES["all"])
    all_proxy = _valid_proxy_url(all_proxy_raw)
    if all_proxy_raw and all_proxy is None:
        invalid.append("ALL_PROXY")
    resolved: dict[str, str | None] = {}
    for scheme in ("http", "https"):
        specific_raw = first(_PROXY_NAMES[scheme])
        specific_proxy = _valid_proxy_url(specific_raw)
        if specific_raw and specific_proxy is None:
            invalid.append(f"{scheme.upper()}_PROXY")
        resolved[scheme] = specific_proxy or all_proxy
    no_proxy = first(_NO_PROXY_NAMES)
    return ProxyPolicy(
        http_proxy=resolved["http"],
        https_proxy=resolved["https"],
        no_proxy=_parse_no_proxy(no_proxy),
        invalid_variables=tuple(dict.fromkeys(invalid)),
    )


def install_proxy_environment(
    policy: ProxyPolicy,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> dict[str, str | None]:
    """Apply normalized proxy variables and return prior values for restoration."""
    target = os.environ if environ is None else environ
    names = (*_PROXY_NAMES["http"], *_PROXY_NAMES["https"], *_PROXY_NAMES["all"], *_NO_PROXY_NAMES)
    previous = {name: target.get(name) for name in names}
    for name in names:
        target.pop(name, None)
    if policy.http_proxy:
        target["HTTP_PROXY"] = policy.http_proxy
    if policy.https_proxy:
        target["HTTPS_PROXY"] = policy.https_proxy
    if policy.no_proxy:
        target["NO_PROXY"] = ",".join(policy.no_proxy)
    return previous


def restore_proxy_environment(
    previous: dict[str, str | None], *, environ: dict[str, str] | os._Environ[str] | None = None
) -> None:
    """Restore environment keys captured by :func:`install_proxy_environment`."""
    target = os.environ if environ is None else environ
    for name, value in previous.items():
        if value is None:
            target.pop(name, None)
        else:
            target[name] = value
