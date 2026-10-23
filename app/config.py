"""Settings loaded from environment variables.

This is the same idea as binding an IOptions<T> from configuration in
ASP.NET: the process environment is the only source, and the rest of the
app receives a filled-in Settings object instead of calling os.getenv
itself.

Nothing about a particular machine (paths, container names, ports) is
hard-coded. Defaults are generic Gluetun / AirVPN names.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.auth import is_bcrypt_hash


# A compose service name is also used as a command argument. Restricting
# the shape means a typo in configuration cannot smuggle extra flags.
_SERVICE_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"


@dataclass(frozen=True)
class Settings:
    """Immutable settings for one process.

    frozen=True means nothing can assign to fields later. That is close
    to a C# record: once built, the object does not change.
    """

    password_hash: str
    session_secret: str
    stack_dir: Path
    env_file: str = ".env"
    compose_file: Path | None = None
    vpn_service: str = "gluetun"
    vpn_container: str = ""
    dependent_services: tuple[str, ...] = ()
    country_env: str = "VPN_SERVER_COUNTRIES"
    city_env: str = "VPN_SERVER_CITIES"
    container_country_env: str = "SERVER_COUNTRIES"
    container_city_env: str = "SERVER_CITIES"
    airvpn_status_url: str = "https://airvpn.org/api/status/"
    # Empty means "do not filter". The default from the environment is
    # Europe; tests and LOCATION_REGIONS= can choose otherwise.
    regions: frozenset[str] = frozenset({"europe"})
    location_cache_seconds: int = 300
    location_fetch_timeout: float = 15.0
    gluetun_proxy_url: str = ""
    ip_info_url: str = "https://ipinfo.io/json"
    compose_timeout: float = 180.0
    health_timeout: float = 120.0
    health_poll_seconds: float = 2.0
    ip_check_timeout: float = 20.0
    secure_cookie: bool = False
    session_max_age: int = 60 * 60 * 24 * 7
    login_max_attempts: int = 5
    login_window_seconds: int = 600
    host: str = "0.0.0.0"
    port: int = 8080

    @property
    def env_path(self) -> Path:
        """Absolute path of the stack's env file."""
        path = Path(self.env_file)
        if not path.is_absolute():
            path = self.stack_dir / path
        return path

    @property
    def recreate_services(self) -> tuple[str, ...]:
        """VPN service first, then containers that share its network."""
        ordered: list[str] = []
        for name in (self.vpn_service, *self.dependent_services):
            if name and name not in ordered:
                ordered.append(name)
        return tuple(ordered)

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> Settings:
        """Read settings from the process environment and reject unsafe values.

        Pass a dict in tests. The running app calls this with no arguments,
        which reads the real environment.
        """
        source = os.environ if env is None else env

        def raw(name: str, default: str | None = None) -> str | None:
            if name not in source:
                return default
            return source[name]

        def text(name: str, default: str = "") -> str:
            value = raw(name)
            if value is None:
                return default
            return value.strip()

        password_hash = text("PASSWORD_HASH")
        session_secret = text("SESSION_SECRET")
        _require_secrets(password_hash, session_secret)

        stack_dir = Path(text("STACK_DIR"))
        if not stack_dir.is_absolute():
            raise SystemExit("STACK_DIR must be an absolute path.")
        if not stack_dir.is_dir():
            raise SystemExit(f"STACK_DIR does not exist or is not a directory: {stack_dir}")

        env_file = text("ENV_FILE", ".env") or ".env"
        compose_file = _optional_path(text("COMPOSE_FILE"), stack_dir)
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = stack_dir / env_path
        if not env_path.is_file():
            raise SystemExit(
                f"Env file not found: {env_path}. "
                "This must be the VPN stack's .env, mounted into the container."
            )
        if compose_file is not None and not compose_file.is_file():
            raise SystemExit(f"COMPOSE_FILE does not exist: {compose_file}")

        vpn_service = _service_name(text("VPN_SERVICE", "gluetun") or "gluetun", "VPN_SERVICE")
        dependents = tuple(
            _service_name(part, "DEPENDENT_SERVICES")
            for part in _csv(text("DEPENDENT_SERVICES"))
        )
        vpn_container = text("VPN_CONTAINER")
        if vpn_container and not _is_container_name(vpn_container):
            raise SystemExit(f"Invalid VPN_CONTAINER: {vpn_container!r}")

        # Missing LOCATION_REGIONS means Europe. An explicit empty value
        # means "show every region".
        if raw("LOCATION_REGIONS") is None:
            regions = frozenset({"europe"})
        else:
            regions = frozenset(part.casefold() for part in _csv(text("LOCATION_REGIONS")))

        return Settings(
            password_hash=password_hash,
            session_secret=session_secret,
            stack_dir=stack_dir,
            env_file=env_file,
            compose_file=compose_file,
            vpn_service=vpn_service,
            vpn_container=vpn_container,
            dependent_services=dependents,
            country_env=_env_name(text("COUNTRY_ENV", "VPN_SERVER_COUNTRIES"), "COUNTRY_ENV"),
            city_env=_env_name(text("CITY_ENV", "VPN_SERVER_CITIES"), "CITY_ENV"),
            container_country_env=_env_name(
                text("CONTAINER_COUNTRY_ENV", "SERVER_COUNTRIES"),
                "CONTAINER_COUNTRY_ENV",
            ),
            container_city_env=_env_name(
                text("CONTAINER_CITY_ENV", "SERVER_CITIES"),
                "CONTAINER_CITY_ENV",
            ),
            airvpn_status_url=text("AIRVPN_STATUS_URL", "https://airvpn.org/api/status/"),
            regions=regions,
            location_cache_seconds=_integer("LOCATION_CACHE_SECONDS", text("LOCATION_CACHE_SECONDS"), 300),
            location_fetch_timeout=_number("LOCATION_FETCH_TIMEOUT", text("LOCATION_FETCH_TIMEOUT"), 15),
            gluetun_proxy_url=text("GLUETUN_PROXY_URL"),
            ip_info_url=text("IP_INFO_URL", "https://ipinfo.io/json"),
            compose_timeout=_number("COMPOSE_TIMEOUT_SECONDS", text("COMPOSE_TIMEOUT_SECONDS"), 180),
            health_timeout=_number("HEALTH_TIMEOUT_SECONDS", text("HEALTH_TIMEOUT_SECONDS"), 120),
            health_poll_seconds=_number("HEALTH_POLL_SECONDS", text("HEALTH_POLL_SECONDS"), 2),
            ip_check_timeout=_number("IP_CHECK_TIMEOUT_SECONDS", text("IP_CHECK_TIMEOUT_SECONDS"), 20),
            secure_cookie=_boolean(text("SECURE_COOKIE"), default=False),
            session_max_age=_integer("SESSION_MAX_AGE_SECONDS", text("SESSION_MAX_AGE_SECONDS"), 60 * 60 * 24 * 7),
            login_max_attempts=_integer("LOGIN_MAX_ATTEMPTS", text("LOGIN_MAX_ATTEMPTS"), 5),
            login_window_seconds=_integer("LOGIN_WINDOW_SECONDS", text("LOGIN_WINDOW_SECONDS"), 600),
            host=text("HOST", "0.0.0.0") or "0.0.0.0",
            port=_integer("PORT", text("PORT"), 8080),
        )


def _require_secrets(password_hash: str, session_secret: str) -> None:
    if not password_hash or "replace-with" in password_hash or not is_bcrypt_hash(password_hash):
        raise SystemExit(
            "PASSWORD_HASH must be a bcrypt hash from `python -m app.hash_password`. "
            "If you put it in a Compose .env file, escape every $ as $$. "
            "Compose otherwise treats $2b as a variable and corrupts the hash."
        )
    if (
        not session_secret
        or "replace-with" in session_secret
        or len(session_secret) < 16
    ):
        raise SystemExit(
            "SESSION_SECRET must be a random string of at least 16 characters. "
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _optional_path(value: str, stack_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = stack_dir / path
    return path


def _service_name(value: str, label: str) -> str:
    if not re.fullmatch(_SERVICE_NAME, value):
        raise SystemExit(f"Invalid {label}: {value!r}")
    return value


def _is_container_name(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is not None


def _env_name(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise SystemExit(f"Invalid {label}: {value!r}")
    return value


def _integer(name: str, value: str, default: int) -> int:
    if value == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from None
    if parsed < 0:
        raise SystemExit(f"{name} must be zero or positive.")
    return parsed


def _number(name: str, value: str, default: float) -> float:
    if value == "":
        return default
    try:
        parsed = float(value)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {value!r}") from None
    if parsed <= 0:
        raise SystemExit(f"{name} must be greater than zero.")
    return parsed


def _boolean(value: str, *, default: bool) -> bool:
    if value == "":
        return default
    return value.casefold() in {"1", "true", "yes", "on"}
