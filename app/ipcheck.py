"""Find the VPN's public address, and the city it is actually in.

Two sources, in this order:

1. Gluetun's control server, GET /v1/publicip/ip. Gluetun has already
   looked the exit up, and the JSON uses a full country name ("Netherlands")
   plus the city. Set GLUETUN_CONTROL_URL to the server's base URL
   (http://host:8000). Recent Gluetun builds require an API key; send it
   as X-API-Key when GLUETUN_CONTROL_API_KEY is set.

2. A public IP service, reached through Gluetun's HTTP proxy
   (GLUETUN_PROXY_URL, HTTPPROXY=on). The request has to leave via the
   VPN, otherwise it would report this app's own address. ipinfo.io often
   returns the country as a 2-letter code ("NL"); the location matcher
   treats that as a code, not a name.

When the stack's country and city are empty, Gluetun picked a server
itself. The page uses this module to name that server. After an explicit
switch, a failure here is only a warning: the containers may already be
on the new location.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.config import Settings

# ipinfo.io uses "ip" / "city" / "country" (country is often "NL").
# ip-api.com uses "query" / "city" / "country" / "countryCode".
# Gluetun's control server uses "public_ip" / "city" / "country"
# (country is the full name). Read whichever of those keys is present.
_IP_KEYS = ("ip", "query", "ip_addr", "ipAddress", "public_ip")
_CITY_KEYS = ("city",)
_REGION_KEYS = ("region", "regionName", "region_name")
_COUNTRY_CODE_KEYS = ("countryCode", "country_code")

# One shot. The control server is on the LAN; if it is down, fall through
# to the proxy instead of waiting out the full IP-check budget.
_CONTROL_TIMEOUT = 5.0


@dataclass(frozen=True)
class ExitStatus:
    """What we could learn about the tunnel's exit.

    `country` is a name ("Netherlands") when the source sent one.
    `country_code` is the 2-letter form ("NL"). A source can set either
    or both. `label` is the text for the exit line under the heading.
    """

    ip: str | None = None
    label: str | None = None
    city: str | None = None
    country: str | None = None
    country_code: str | None = None
    error: str | None = None


def resolve_exit(settings: Settings, *, timeout: float | None = None) -> ExitStatus:
    """Best place we can see the VPN is actually in.

    The control server wins when it names a city or a country, because
    that answer was resolved inside the tunnel and uses a full country
    name. Otherwise use the proxy lookup. `timeout` caps the proxy
    retries; the page render passes a short one so a dead proxy does not
    hold the location list. /status uses the full IP_CHECK_TIMEOUT_SECONDS.
    """
    control = _fetch_control(settings) if settings.gluetun_control_url else None
    if control is not None and _has_place(control):
        return control

    proxy = fetch_exit_info(settings, timeout=timeout)
    # The control server sometimes returns only the address (older
    # builds). Keep that address when the proxy lookup has none.
    if control is not None and control.ip and not proxy.ip:
        return ExitStatus(
            ip=control.ip,
            label=proxy.label,
            city=proxy.city,
            country=proxy.country,
            country_code=proxy.country_code,
            error=proxy.error,
        )
    return proxy


def fetch_exit_info(settings: Settings, *, timeout: float | None = None) -> ExitStatus:
    """Try until the budget runs out. The proxy may come up a moment after "healthy"."""
    if not settings.gluetun_proxy_url:
        return ExitStatus(error="Exit IP was not checked because GLUETUN_PROXY_URL is not set.")
    if not settings.ip_info_url:
        return ExitStatus(error="Exit IP was not checked because IP_INFO_URL is not set.")

    budget = settings.ip_check_timeout if timeout is None else timeout
    deadline = time.monotonic() + budget
    last_error = "timed out"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ExitStatus(error=f"Exit IP check failed: {last_error}")
        try:
            return _fetch_once(settings, min(10.0, remaining))
        except (httpx.HTTPError, ValueError, RuntimeError, OSError) as exc:
            # The switch has already happened. Any failure here is a warning.
            last_error = _short(str(exc))
        if time.monotonic() >= deadline:
            return ExitStatus(error=f"Exit IP check failed: {last_error}")
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def parse_ip_payload(payload: object) -> tuple[str | None, str | None]:
    """Pull an address and a place name out of a JSON object or a bare IP string."""
    status = parse_exit_payload(payload)
    return status.ip, status.label


def parse_exit_payload(payload: object) -> ExitStatus:
    """Same as parse_ip_payload, plus city and country kept as their own fields.

    A 2-letter `country` value is stored as a country code. ipinfo.io
    sends "NL"; ip-api.com and Gluetun send "Netherlands".
    """
    if isinstance(payload, str):
        text = payload.strip()
        if _looks_like_ip(text):
            return ExitStatus(ip=text)
        return ExitStatus()
    if not isinstance(payload, dict):
        return ExitStatus()

    ip = _first(payload, _IP_KEYS)
    if ip and not _looks_like_ip(ip):
        # Some APIs nest the address. Ignore a value that is clearly not one.
        ip = None
    city = _first(payload, _CITY_KEYS)
    country_code = _first(payload, _COUNTRY_CODE_KEYS)
    country = _first(payload, ("country_name",))
    country_field = _first(payload, ("country",))
    if country_field and _is_country_code(country_field):
        country_code = country_code or country_field.upper()
    elif country_field:
        country = country or country_field
    if country_code:
        country_code = country_code.upper()

    # Region is a state or canton. It belongs on the exit line, not in the
    # city we match against the AirVPN list.
    region = _first(payload, _REGION_KEYS)
    parts: list[str] = []
    for part in (city, region, country or country_code):
        if part and part not in parts:
            parts.append(part)
    label = ", ".join(parts) if parts else None
    return ExitStatus(
        ip=ip,
        label=label,
        city=city,
        country=country,
        country_code=country_code,
    )


def _fetch_control(settings: Settings) -> ExitStatus:
    """GET {GLUETUN_CONTROL_URL}/v1/publicip/ip. Errors become an empty status."""
    url = settings.gluetun_control_url.rstrip("/") + "/v1/publicip/ip"
    headers = {"Accept": "application/json"}
    if settings.gluetun_control_api_key:
        headers["X-API-Key"] = settings.gluetun_control_api_key
    try:
        with httpx.Client(timeout=_CONTROL_TIMEOUT, follow_redirects=True, trust_env=False) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        return ExitStatus(error=_short(str(exc)))
    status = parse_exit_payload(payload)
    if _has_place(status) or status.ip:
        return status
    return ExitStatus(error="Gluetun's control server did not include a location.")


def _fetch_once(settings: Settings, timeout: float) -> ExitStatus:
    # trust_env=False: ignore HTTP_PROXY from the environment and use only
    # the proxy this app was configured with.
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
    status = parse_exit_payload(payload)
    if not status.ip:
        raise RuntimeError("the IP service response did not include an address")
    return status


def _has_place(status: ExitStatus) -> bool:
    return bool(status.city or status.country or status.country_code)


def _first(payload: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_country_code(value: str) -> bool:
    return len(value) == 2 and value.isalpha()


def _looks_like_ip(value: str) -> bool:
    if len(value) > 64 or any(char.isspace() for char in value):
        return False
    return "." in value or ":" in value


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
