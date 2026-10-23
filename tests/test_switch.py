"""Switch orchestration, with Docker and the network replaced by fakes.

subprocess.run is swapped out so the tests never talk to a daemon. The
HTTP test uses FastAPI's TestClient, which is an in-process browser: it
sends requests without opening a port.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import Settings
from app.dockerctl import compose_up_command, docker_subprocess_env, run_command
from app.errors import BadRequestError, DockerError, SwitchBusyError
from app.ipcheck import ExitStatus, parse_ip_payload
from app.main import create_app
from app.switcher import SwitchGate, perform_switch

PASSWORD_HASH = hash_password("test-password")

SAMPLE = {
    "servers": [
        {"country_name": "Germany", "location": "Frankfurt", "continent": "Europe", "health": "ok"},
        {"country_name": "Netherlands", "location": "Amsterdam", "continent": "Europe", "health": "ok"},
        {"country_name": "United States", "location": "Phoenix, Arizona", "continent": "America", "health": "ok"},
        {"country_name": "Sweden", "location": "Stockholm", "continent": "Europe", "health": "down"},
    ]
}


def test_compose_command_recreates_the_vpn_and_its_dependents(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    command = compose_up_command(settings)
    assert command[:4] == ["docker", "compose", "--project-directory", str(tmp_path)]
    assert command[command.index("up") :] == [
        "up",
        "-d",
        "--force-recreate",
        "gluetun",
        "qbittorrent",
    ]


def test_compose_environment_does_not_carry_app_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PASSWORD_HASH", "should-not-leak")
    monkeypatch.setenv("SESSION_SECRET", "should-not-leak")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "vpnstack")
    env = docker_subprocess_env()
    assert "PASSWORD_HASH" not in env
    assert "SESSION_SECRET" not in env
    assert env["COMPOSE_PROJECT_NAME"] == "vpnstack"


def test_docker_errors_redact_secret_looking_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "WIREGUARD_PRIVATE_KEY=abc\nthe city is not valid\n",
        )

    monkeypatch.setattr("app.dockerctl.subprocess.run", fake)
    with pytest.raises(DockerError) as raised:
        run_command(["docker", "compose"], cwd=None, timeout=5)
    message = raised.value.message
    assert "abc" not in message
    assert "the city is not valid" in message


def test_unknown_city_does_not_touch_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    _patch_locations(monkeypatch)
    before = settings.env_path.read_text(encoding="utf-8")
    with pytest.raises(BadRequestError, match="not in the current list"):
        perform_switch(settings, _cache(settings), "Germany", "Not A Real City")
    assert settings.env_path.read_text(encoding="utf-8") == before


def test_switch_updates_env_and_reports_a_docker_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    _patch_locations(monkeypatch)
    monkeypatch.setattr("app.switcher.read_current", lambda s: _current("Netherlands", "Amsterdam"))

    def fail_recreate(settings: Settings) -> None:
        raise DockerError("compose failed", status_code=502)

    monkeypatch.setattr("app.switcher.recreate", fail_recreate)

    outcome = perform_switch(settings, _cache(settings), "Germany", "Frankfurt")

    text = settings.env_path.read_text(encoding="utf-8")
    assert "OTHER=keep" in text
    assert "# keep" in text
    assert "VPN_SERVER_COUNTRIES=Germany" in text
    assert "VPN_SERVER_CITIES=Frankfurt" in text
    assert outcome.ok is False
    assert outcome.env_updated is True
    assert "env file now points at" in (outcome.detail or "")
    assert outcome.status_code == 502


def test_already_selected_location_does_not_recreate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    _patch_locations(monkeypatch)
    monkeypatch.setattr("app.switcher.read_current", lambda s: _current("Germany", "Frankfurt"))
    called = {"recreate": False}
    monkeypatch.setattr("app.switcher.recreate", lambda s: called.__setitem__("recreate", True))

    outcome = perform_switch(settings, _cache(settings), "Germany", "Frankfurt")

    assert outcome.ok is True
    assert outcome.unchanged is True
    assert called["recreate"] is False
    assert "VPN_SERVER_COUNTRIES=Netherlands" in settings.env_path.read_text(encoding="utf-8")


def test_gate_rejects_a_second_switch_until_the_first_finishes() -> None:
    async def run() -> None:
        gate = SwitchGate()
        await gate.acquire()
        with pytest.raises(SwitchBusyError):
            await gate.acquire()
        await gate.release()
        await gate.acquire()
        await gate.release()

    asyncio.run(run())


def test_ip_payload_accepts_the_common_shapes() -> None:
    assert parse_ip_payload({"ip": "203.0.113.5", "city": "Amsterdam", "country": "NL"}) == (
        "203.0.113.5",
        "Amsterdam, NL",
    )
    assert parse_ip_payload({"query": "203.0.113.8", "city": "Frankfurt", "country": "Germany"})[0] == "203.0.113.8"
    assert parse_ip_payload("203.0.113.9") == ("203.0.113.9", None)
    assert parse_ip_payload({"ip": "not an address"}) == (None, None)


def test_login_and_switch_through_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if "inspect" in command:
            fmt = command[command.index("--format") + 1]
            if "Health" in fmt:
                stdout = "healthy running\n"
            else:
                stdout = "SERVER_COUNTRIES=Netherlands\nSERVER_CITIES=Amsterdam\n"
            return subprocess.CompletedProcess(command, 0, stdout, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.locations.fetch_status", lambda url, timeout: SAMPLE)
    monkeypatch.setattr("app.dockerctl.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.switcher.fetch_exit_info",
        lambda s: ExitStatus(ip="203.0.113.10", label="Frankfurt, DE"),
    )

    app = create_app(settings)
    with TestClient(app) as client:
        anonymous = client.get("/", follow_redirects=False)
        assert anonymous.status_code == 303
        assert anonymous.headers["location"] == "/login"

        login_page = client.get("/login")
        assert login_page.status_code == 200
        wrong = client.post(
            "/login",
            data={"csrf_token": _csrf(login_page.text), "password": "nope"},
            follow_redirects=False,
        )
        assert wrong.status_code == 401

        signed_in = client.post(
            "/login",
            data={"csrf_token": _csrf(wrong.text), "password": "test-password"},
            follow_redirects=True,
        )
        assert signed_in.status_code == 200
        assert "Amsterdam" in signed_in.text
        assert "Frankfurt" in signed_in.text
        assert "Phoenix" not in signed_in.text
        # Germany sorts before the Netherlands. The header also names the
        # current city, so compare the country headings rather than the
        # first time each city string appears.
        assert signed_in.text.index("<h2>Germany</h2>") < signed_in.text.index("<h2>Netherlands</h2>")

        denied = client.post(
            "/switch",
            data={"csrf_token": "not-the-token", "country": "Germany", "city": "Frankfurt"},
            headers={"Accept": "application/json"},
        )
        assert denied.status_code == 403
        assert "VPN_SERVER_COUNTRIES=Netherlands" in settings.env_path.read_text(encoding="utf-8")

        switched = client.post(
            "/switch",
            data={"csrf_token": _csrf(signed_in.text), "country": "Germany", "city": "Frankfurt"},
            headers={"Accept": "application/json"},
        )
        assert switched.status_code == 200
        body = switched.json()
        assert body["ok"] is True
        assert body["city"] == "Frankfurt"
        assert body["exit_ip"] == "203.0.113.10"

    text = settings.env_path.read_text(encoding="utf-8")
    assert "# keep" in text
    assert "OTHER=keep" in text
    assert "VPN_SERVER_COUNTRIES=Germany" in text
    assert "VPN_SERVER_CITIES=Frankfurt" in text
    recreate = [command for command in commands if "up" in command]
    assert recreate
    assert recreate[-1][recreate[-1].index("up") :] == [
        "up",
        "-d",
        "--force-recreate",
        "gluetun",
        "qbittorrent",
    ]


def _settings(tmp_path: Path) -> Settings:
    env = tmp_path / ".env"
    env.write_text(
        "# keep\nOTHER=keep\nVPN_SERVER_COUNTRIES=Netherlands\nVPN_SERVER_CITIES=Amsterdam\n",
        encoding="utf-8",
    )
    return Settings(
        password_hash=PASSWORD_HASH,
        session_secret="test-session-secret-value",
        stack_dir=tmp_path,
        vpn_container="gluetun",
        dependent_services=("qbittorrent",),
        health_timeout=2,
        health_poll_seconds=0.01,
        ip_check_timeout=2,
        gluetun_proxy_url="http://proxy.example:8888",
    )


def _patch_locations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.locations.fetch_status", lambda url, timeout: SAMPLE)


def _cache(settings: Settings):
    from app.locations import LocationCache

    return LocationCache(settings)


class _Current:
    def __init__(self, country: str, city: str) -> None:
        self.country = country
        self.city = city
        self.warning = None


def _current(country: str, city: str) -> _Current:
    return _Current(country, city)


def _csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]
