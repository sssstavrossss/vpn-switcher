"""Settings come from the environment and fail closed when secrets are missing."""

from pathlib import Path

import pytest

from app.auth import hash_password
from app.config import Settings

PASSWORD_HASH = hash_password("test-password")


def test_defaults_and_region_filter(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    base = {
        "PASSWORD_HASH": PASSWORD_HASH,
        "SESSION_SECRET": "test-session-secret-value",
        "STACK_DIR": str(tmp_path),
    }
    settings = Settings.from_env(base)
    assert settings.regions == frozenset({"europe"})
    assert settings.vpn_service == "gluetun"
    assert settings.country_env == "VPN_SERVER_COUNTRIES"
    assert settings.city_env == "VPN_SERVER_CITIES"
    assert settings.container_country_env == "SERVER_COUNTRIES"
    assert settings.env_path == tmp_path / ".env"
    assert settings.gluetun_control_url == ""
    assert settings.gluetun_control_api_key == ""

    control = dict(
        base,
        GLUETUN_CONTROL_URL="http://gluetun:8000",
        GLUETUN_CONTROL_API_KEY="secret-key",
    )
    parsed_control = Settings.from_env(control)
    assert parsed_control.gluetun_control_url == "http://gluetun:8000"
    assert parsed_control.gluetun_control_api_key == "secret-key"

    all_regions = dict(base, LOCATION_REGIONS="")
    assert Settings.from_env(all_regions).regions == frozenset()

    both = dict(base, LOCATION_REGIONS="Europe, America")
    assert Settings.from_env(both).regions == frozenset({"europe", "america"})

    custom = dict(
        base,
        VPN_SERVICE="wireguard",
        DEPENDENT_SERVICES="qbittorrent, other-app",
        COUNTRY_ENV="SERVER_COUNTRIES",
    )
    parsed = Settings.from_env(custom)
    assert parsed.recreate_services == ("wireguard", "qbittorrent", "other-app")
    assert parsed.country_env == "SERVER_COUNTRIES"


def test_placeholder_secrets_are_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        Settings.from_env(
            {
                "PASSWORD_HASH": "replace-with-output-of-hash-password",
                "SESSION_SECRET": "test-session-secret-value",
                "STACK_DIR": str(tmp_path),
            }
        )
    with pytest.raises(SystemExit):
        Settings.from_env(
            {
                "PASSWORD_HASH": PASSWORD_HASH,
                "SESSION_SECRET": "too-short",
                "STACK_DIR": str(tmp_path),
            }
        )


def test_stack_dir_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        Settings.from_env(
            {
                "PASSWORD_HASH": PASSWORD_HASH,
                "SESSION_SECRET": "test-session-secret-value",
                "STACK_DIR": "relative/path",
            }
        )
