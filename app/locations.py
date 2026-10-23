"""Load AirVPN's public server list and turn it into country/city rows.

The status document is https://airvpn.org/api/status/. Each server has a
country, a location (city), a continent, and a health flag. Many servers
share one city, so the page shows one row per city.

Gluetun reads the same API and then deletes commas from the location,
because SERVER_CITIES is itself a comma-separated list. "Phoenix, Arizona"
must be written as "Phoenix Arizona" or Gluetun will reject it. The
transformation below is the one in Gluetun's AirVPN updater:

    city = strings.ReplaceAll(location, ", ", " ")
    city = strings.ReplaceAll(city, ",", "")
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.errors import LocationError


@dataclass(frozen=True)
class Location:
    """One city the user can select.

    `city` is the value written to the env file (Gluetun's spelling).
    `label` is AirVPN's own text, which is what the page shows. For most
    European cities the two are identical.
    """

    country: str
    city: str
    label: str
    region: str


def gluetun_city(location: str) -> str:
    """Spell a city the way Gluetun's AirVPN server list spells it."""
    city = location.replace(", ", " ")
    city = city.replace(",", "")
    return city.strip()


def same_place(country_a: str, city_a: str, country_b: str, city_b: str) -> bool:
    """True when two country/city pairs name the same Gluetun location."""
    return (
        country_a.strip().casefold() == country_b.strip().casefold()
        and gluetun_city(city_a).casefold() == gluetun_city(city_b).casefold()
    )


def find_location(locations: list[Location], country: str, city: str) -> Location | None:
    for location in locations:
        if same_place(location.country, location.city, country, city):
            return location
    return None


def parse_locations(payload: dict, regions: frozenset[str]) -> list[Location]:
    """Filter, de-duplicate, and sort the status document.

    `regions` holds lower-case continent names. An empty set keeps every
    continent. Servers Gluetun would skip (health other than "ok") are
    skipped here too, so the page does not offer a city that is down.
    """
    servers = payload.get("servers")
    if not isinstance(servers, list):
        raise LocationError("AirVPN status response has no servers list.")

    found: dict[tuple[str, str], Location] = {}
    for server in servers:
        if not isinstance(server, dict):
            continue
        if str(server.get("health", "")).casefold() != "ok":
            continue
        country = str(server.get("country_name") or "").strip()
        raw_city = str(server.get("location") or "").strip()
        region = str(server.get("continent") or "").strip()
        if not country or not raw_city:
            continue
        if regions and region.casefold() not in regions:
            continue
        city = gluetun_city(raw_city)
        if not city:
            continue
        key = (country.casefold(), city.casefold())
        if key in found:
            continue
        found[key] = Location(country=country, city=city, label=raw_city, region=region)

    return sorted(found.values(), key=lambda item: (item.country.casefold(), item.label.casefold()))


def fetch_status(url: str, timeout: float) -> dict:
    """GET the status URL. trust_env is off so HTTP_PROXY cannot redirect this call."""
    headers = {"Accept": "application/json", "User-Agent": "vpn-switcher"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise LocationError(f"Couldn't reach the AirVPN status API: {exc}") from exc
    except ValueError as exc:
        raise LocationError("AirVPN status API did not return JSON.") from exc
    if not isinstance(payload, dict):
        raise LocationError("AirVPN status API returned an unexpected document.")
    return payload


class LocationCache:
    """Remember the parsed list for a short time.

    A phone reload should not hit AirVPN on every request. The lock is
    held during the fetch so two requests cannot download the list at
    once (a "stampede"). If a later fetch fails and a previous list is
    still in memory, that previous list is used.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._locations: list[Location] | None = None
        self._loaded_at = 0.0
        # After a failed refresh, don't call AirVPN again until this time.
        self._retry_not_before = 0.0

    def get(self) -> list[Location]:
        with self._lock:
            now = time.monotonic()
            if self._fresh(now):
                assert self._locations is not None
                return list(self._locations)
            if self._locations is not None and now < self._retry_not_before:
                return list(self._locations)
            try:
                payload = fetch_status(
                    self._settings.airvpn_status_url,
                    self._settings.location_fetch_timeout,
                )
                locations = parse_locations(payload, self._settings.regions)
            except LocationError:
                if self._locations is not None:
                    self._retry_not_before = now + 30
                    return list(self._locations)
                raise
            self._locations = locations
            self._loaded_at = now
            self._retry_not_before = 0.0
            return list(locations)

    def _fresh(self, now: float) -> bool:
        return (
            self._locations is not None
            and now - self._loaded_at < self._settings.location_cache_seconds
        )
