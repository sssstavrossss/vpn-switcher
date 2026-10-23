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
from app.ipcheck import fetch_exit_info
from app.locations import LocationCache, find_location, same_place

log = logging.getLogger(__name__)

_MAX_LOCATION_LENGTH = 80


@dataclass
class CurrentLocation:
    country: str
    city: str
    warning: str | None = None


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
