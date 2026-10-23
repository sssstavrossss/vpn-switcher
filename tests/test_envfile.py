"""Tests for editing a Compose .env file without disturbing the rest of it."""

import os
import stat
from pathlib import Path

import pytest

from app.envfile import EnvEditError, read_env, update_env_file


def _write(path: Path, text: str, mode: int = 0o640) -> None:
    # newline="" writes the string's line endings as they are.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    path.chmod(mode)


def test_replaces_location_keys_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = (
        "# stack env\n"
        "# VPN_SERVER_COUNTRIES=do not touch this comment\n"
        "\n"
        "OTHER=keep me\n"
        "export QUOTED=\"hash # not a comment\"\n"
        "VPN_SERVER_COUNTRIES=Sweden\n"
        "  VPN_SERVER_CITIES=Stockholm\n"
    )
    _write(path, original)

    update_env_file(
        path,
        {"VPN_SERVER_COUNTRIES": "Netherlands", "VPN_SERVER_CITIES": "Alblasserdam"},
    )

    assert path.read_text(encoding="utf-8") == (
        "# stack env\n"
        "# VPN_SERVER_COUNTRIES=do not touch this comment\n"
        "\n"
        "OTHER=keep me\n"
        "export QUOTED=\"hash # not a comment\"\n"
        "VPN_SERVER_COUNTRIES=Netherlands\n"
        "  VPN_SERVER_CITIES=Alblasserdam\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.stat().st_uid == os.getuid()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_appends_missing_keys_and_quotes_spaces(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    _write(path, "# comment\nFOO=bar\n", mode=0o600)

    update_env_file(
        path,
        {"VPN_SERVER_COUNTRIES": "United States", "VPN_SERVER_CITIES": "Phoenix Arizona"},
    )

    text = path.read_text(encoding="utf-8")
    assert text == (
        "# comment\n"
        "FOO=bar\n"
        "VPN_SERVER_COUNTRIES='United States'\n"
        "VPN_SERVER_CITIES='Phoenix Arizona'\n"
    )
    assert read_env(path)["VPN_SERVER_CITIES"] == "Phoenix Arizona"
    assert read_env(path)["VPN_SERVER_COUNTRIES"] == "United States"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_updates_every_uncommented_copy_and_last_one_wins_on_read(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    _write(path, "VPN_SERVER_COUNTRIES=A\nVPN_SERVER_COUNTRIES=B\n")
    assert read_env(path)["VPN_SERVER_COUNTRIES"] == "B"

    update_env_file(path, {"VPN_SERVER_COUNTRIES": "Germany"})
    assert path.read_text(encoding="utf-8") == (
        "VPN_SERVER_COUNTRIES=Germany\n"
        "VPN_SERVER_COUNTRIES=Germany\n"
    )


def test_preserves_export_prefix_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    _write(path, "export VPN_SERVER_COUNTRIES=Sweden\r\nOTHER=1\r\n")

    update_env_file(path, {"VPN_SERVER_COUNTRIES": "Germany"})

    with path.open(encoding="utf-8", newline="") as handle:
        assert handle.read() == "export VPN_SERVER_COUNTRIES=Germany\r\nOTHER=1\r\n"


def test_reads_quoted_values_and_ignores_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    _write(
        path,
        "\n".join(
            [
                "# VPN_SERVER_COUNTRIES=Ignore",
                "VPN_SERVER_COUNTRIES=\"Netherlands\"",
                "VPN_SERVER_CITIES='Phoenix Arizona' # note",
                "PLAIN=value # keep",
                "",
            ]
        ),
    )
    values = read_env(path)
    assert values["VPN_SERVER_COUNTRIES"] == "Netherlands"
    assert values["VPN_SERVER_CITIES"] == "Phoenix Arizona"
    assert values["PLAIN"] == "value"


def test_rejects_a_newline_and_leaves_the_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    _write(path, "FOO=bar\n")

    with pytest.raises(EnvEditError):
        update_env_file(path, {"VPN_SERVER_COUNTRIES": "Bad\nINJECTED=1"})

    assert path.read_text(encoding="utf-8") == "FOO=bar\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(EnvEditError):
        update_env_file(tmp_path / ".env", {"VPN_SERVER_COUNTRIES": "Germany"})
