"""Apply one location change from start to finish.

The web route is thin. This module is the sequence:

1. Confirm the city is in the filtered AirVPN list (the browser is not
   trusted to send an arbitrary string into the env file).
2. If that city is already the running one, do nothing.
3. Rewrite the two location keys in the stack's .env file.
4. Recreate the VPN service and the services that share its network.
5. Wait until Gluetun is healthy.
6. Look up the public exit address through Gluetun. A failure there is
   reported, but the switch has already happened.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.config import Settings
from app.dockerctl import inspect_env, recreate, resolve_container, wait_until_healthy
from app.envfile import read_env, update_env_file
from app.errors import BadRequestError, DockerError, EnvEditError, LocationError, SwitchBusyError
from app.ipcheck import ExitStatus, fetch_exit_info
from app.locations import Location, LocationCache, find_location, match_observed, same_place

log = logging.getLogger(__name__)

_MAX_LOCATION_LENGTH = 80


@dataclass
class CurrentLocation:
    country: str
    city: str
    warning: str | None = None


@dataclass(frozen=True)
class DisplayLocation:
    """What the heading and the selected switch should show.

    `auto_picked` means the country and city env vars are empty and the
    heading came from the tunnel's exit, not from a choice the user saved.
    `matched` means that city is a row in the list, so its switch is on.
    """

    heading: str
    note: str | None
    country: str
    city: str
    label: str
    auto_picked: bool
    matched: bool


@dataclass
class SwitchOutcome:
    ok: bool
    country: str = ""
    city: str = ""
    label: str = ""
    exit_ip: str | None = None
    exit_label: str | None = None
    detail: str | None = None
    env_updated: bool = False
    unchanged: bool = False
    status_code: int = 200

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "country": self.country,
            "city": self.city,
            "label": self.label,
            "exit_ip": self.exit_ip,
            "exit_label": self.exit_label,
            "detail": self.detail,
            "env_updated": self.env_updated,
            "unchanged": self.unchanged,
        }


class SwitchGate:
    """Let one switch run, and reject any other until it finishes.

    The flag lives in this process. Do not run multiple server workers
    or two switches could edit the env file at the same time.
    """

    def __init__(self) -> None:
        self._mutex = asyncio.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    async def acquire(self) -> None:
        async with self._mutex:
            if self._busy:
                raise SwitchBusyError("A switch is already running. Wait for it to finish.")
            self._busy = True

    async def release(self) -> None:
        async with self._mutex:
            self._busy = False


def read_current(settings: Settings) -> CurrentLocation:
    """Prefer the running container. Fall back to the env file if Docker cannot be read.

    When both are present and they disagree, the container wins (that is
    the tunnel that is actually up) and the warning says the file differs.
    """
    file_country, file_city, file_warning = _from_file(settings)
    try:
        container = resolve_container(settings)
        env = inspect_env(container)
    except DockerError as exc:
        warning = f"Couldn't read the running container ({exc.message}). Showing the env file instead."
        if file_warning:
            warning = f"{file_warning} {warning}"
        return CurrentLocation(file_country, file_city, warning)

    container_country = _value(env, settings.container_country_env, settings.country_env)
    container_city = _value(env, settings.container_city_env, settings.city_env)
    if not container_country and not container_city:
        return CurrentLocation(file_country, file_city, file_warning)

    warning = file_warning
    if (file_country or file_city) and not same_place(
        file_country, file_city, container_country, container_city
    ):
        warning = (
            f"The running container is {_place(container_city, container_country)}, "
            f"but the env file is set to {_place(file_city, file_country)}."
        )
    return CurrentLocation(container_country, container_city, warning)


def choose_display(
    configured: CurrentLocation,
    observed: ExitStatus | None,
    locations: list[Location],
) -> DisplayLocation:
    """Heading for the page.

    A saved country or city always wins. Those are what the next restart
    will use, and what "already on this location" compares against.
    When both are empty, Gluetun picked a server in the region by itself.
    Name that server from `observed` (control server, or the exit lookup)
    and turn on the matching switch. If the city cannot be read, say so.
    "No location selected" would be wrong: the tunnel is already up.
    """
    if configured.country or configured.city:
        match = find_location(locations, configured.country, configured.city)
        if match is not None:
            return DisplayLocation(
                heading=f"{match.label}, {match.country}",
                note=None,
                country=match.country,
                city=match.city,
                label=match.label,
                auto_picked=False,
                matched=True,
            )
        heading = ", ".join(part for part in (configured.city, configured.country) if part)
        return DisplayLocation(
            heading=heading,
            note=None,
            country=configured.country,
            city=configured.city,
            label=configured.city,
            auto_picked=False,
            matched=False,
        )

    if observed is not None and (observed.city or observed.country or observed.country_code):
        match = match_observed(locations, observed.city, observed.country, observed.country_code)
        if match is not None:
            return DisplayLocation(
                heading=f"{match.label}, {match.country}",
                note="Auto-picked",
                country=match.country,
                city=match.city,
                label=match.label,
                auto_picked=True,
                matched=True,
            )
        country = observed.country or observed.country_code or ""
        if observed.city and country:
            heading = f"{observed.city}, {country}"
        else:
            heading = observed.city or country
        return DisplayLocation(
            heading=heading,
            note="Auto-picked",
            country=observed.country or "",
            city=observed.city or "",
            label=observed.city or "",
            auto_picked=True,
            matched=False,
        )

    return _unknown_display(observed)


def _unknown_display(observed: ExitStatus | None) -> DisplayLocation:
    if observed is not None and observed.ip:
        note = f"Exit {observed.ip}. The city could not be read from the connection."
    elif observed is not None and observed.error:
        note = "No country or city is set, and the exit location could not be read. " + observed.error
    else:
        note = "No country or city is set, and the exit location could not be read."
    return DisplayLocation(
        heading="Location unknown",
        note=note,
        country="",
        city="",
        label="",
        auto_picked=False,
        matched=False,
    )


def perform_switch(
    settings: Settings,
    cache: LocationCache,
    country: str,
    city: str,
) -> SwitchOutcome:
    """Run the switch. Called in a worker thread so Docker's wait does not freeze the server."""
    country = country.strip()
    city = city.strip()
    _validate_location_text(country, city)

    try:
        locations = cache.get()
    except LocationError as exc:
        raise BadRequestError(f"Couldn't load the location list, so nothing was changed. {exc.message}") from exc

    match = find_location(locations, country, city)
    if match is None:
        raise BadRequestError("That location is not in the current list. Reload the page and pick another.")

    current = read_current(settings)
    if same_place(current.country, current.city, match.country, match.city):
        return SwitchOutcome(
            ok=True,
            country=match.country,
            city=match.city,
            label=match.label,
            unchanged=True,
            detail="Already on this location.",
        )

    update_env_file(
        settings.env_path,
        {settings.country_env: match.country, settings.city_env: match.city},
    )
    log.info("Set %s to %s, %s", settings.city_env, match.city, match.country)

    try:
        recreate(settings)
        wait_until_healthy(settings)
    except DockerError as exc:
        log.warning("Recreate failed after the env file was updated: %s", exc.message)
        return SwitchOutcome(
            ok=False,
            country=match.country,
            city=match.city,
            label=match.label,
            detail=(
                f"The env file now points at {match.label}, {match.country}, "
                f"but the containers were not recreated successfully. {exc.message}"
            ),
            env_updated=True,
            status_code=exc.status_code,
        )

    exit_status = fetch_exit_info(settings)
    log.info("Switch to %s finished (exit %s)", match.city, exit_status.ip or "unknown")
    return SwitchOutcome(
        ok=True,
        country=match.country,
        city=match.city,
        label=match.label,
        exit_ip=exit_status.ip,
        exit_label=exit_status.label,
        detail=exit_status.error,
        env_updated=True,
    )


def _validate_location_text(country: str, city: str) -> None:
    if not country or not city:
        raise BadRequestError("Choose a country and a city.")
    for value in (country, city):
        if len(value) > _MAX_LOCATION_LENGTH:
            raise BadRequestError("That location name is unexpectedly long.")
        if any(char in value for char in ("\n", "\r", "\x00", "'")):
            raise BadRequestError("That location contains a character the env file cannot store safely.")


def _from_file(settings: Settings) -> tuple[str, str, str | None]:
    try:
        values = read_env(settings.env_path)
    except EnvEditError as exc:
        return "", "", exc.message
    return (
        values.get(settings.country_env, "").strip(),
        values.get(settings.city_env, "").strip(),
        None,
    )


def _value(env: dict[str, str], primary: str, fallback: str) -> str:
    return (env.get(primary) or env.get(fallback) or "").strip()


def _place(city: str, country: str) -> str:
    if city and country:
        return f"{city}, {country}"
    return city or country or "unset"
