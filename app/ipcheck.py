"""Ask a public IP service through Gluetun's HTTP proxy.

The request has to leave via the VPN, otherwise it would report this
app's own address and look like a successful switch. Set
GLUETUN_PROXY_URL to Gluetun's HTTP proxy (HTTPPROXY=on). When that is
unset, the check is skipped and the switch itself still counts as done.

Failure here is a warning, not a failed switch: the containers may
already be on the new location.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.config import Settings

# ipinfo.io uses "ip" / "city" / "country". ip-api.com uses "query" / "city"
# / "country". ifconfig.co uses "ip" / "city" / "country". Read whichever
# of those keys is present.
_IP_KEYS = ("ip", "query", "ip_addr", "ipAddress")
_CITY_KEYS = ("city",)
_REGION_KEYS = ("region", "regionName", "region_name")
_COUNTRY_KEYS = ("country", "country_name", "countryCode")


@dataclass(frozen=True)
class ExitStatus:
    ip: str | None = None
    label: str | None = None
    error: str | None = None


def fetch_exit_info(settings: Settings) -> ExitStatus:
    """Try until IP_CHECK_TIMEOUT_SECONDS. The proxy may come up a moment after "healthy"."""
    if not settings.gluetun_proxy_url:
        return ExitStatus(error="Exit IP was not checked because GLUETUN_PROXY_URL is not set.")
    if not settings.ip_info_url:
        return ExitStatus(error="Exit IP was not checked because IP_INFO_URL is not set.")

    deadline = time.monotonic() + settings.ip_check_timeout
    last_error = "timed out"
    while True:
        try:
            return _fetch_once(settings)
        except (httpx.HTTPError, ValueError, RuntimeError, OSError) as exc:
            # The switch has already happened. Any failure here is a warning.
            last_error = _short(str(exc))
        if time.monotonic() >= deadline:
            return ExitStatus(error=f"Exit IP check failed: {last_error}")
        time.sleep(1)


def parse_ip_payload(payload: object) -> tuple[str | None, str | None]:
    """Pull an address and a place name out of a JSON object or a bare IP string."""
    if isinstance(payload, str):
        text = payload.strip()
        if _looks_like_ip(text):
            return text, None
        return None, None
    if not isinstance(payload, dict):
        return None, None
    ip = _first(payload, _IP_KEYS)
    parts: list[str] = []
    for key_group in (_CITY_KEYS, _REGION_KEYS, _COUNTRY_KEYS):
        part = _first(payload, key_group)
        if part and part not in parts:
            parts.append(part)
    label = ", ".join(parts) if parts else None
    if ip and not _looks_like_ip(ip):
        # Some APIs nest the address. Ignore a value that is clearly not one.
        ip = None
    return ip, label


def _fetch_once(settings: Settings) -> ExitStatus:
    # trust_env=False: ignore HTTP_PROXY from the environment and use only
    # the proxy this app was configured with.
    timeout = min(10.0, settings.ip_check_timeout)
    with httpx.Client(
        proxy=settings.gluetun_proxy_url,
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
    ) as client:
        response = client.get(settings.ip_info_url, headers={"Accept": "application/json, text/plain"})
        response.raise_for_status()
        payload: object
        content_type = response.headers.get("content-type", "")
        body = response.text
        if "json" in content_type or body.lstrip().startswith(("{", "[")):
            payload = response.json()
        else:
            payload = body
    ip, label = parse_ip_payload(payload)
    if not ip:
        raise RuntimeError("the IP service response did not include an address")
    return ExitStatus(ip=ip, label=label)


def _first(payload: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _looks_like_ip(value: str) -> bool:
    if len(value) > 64 or any(char.isspace() for char in value):
        return False
    return "." in value or ":" in value


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
