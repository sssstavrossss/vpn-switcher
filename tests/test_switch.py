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
from app.ipcheck import ExitStatus, parse_exit_payload, parse_ip_payload, resolve_exit
from app.locations import Location
from app.main import create_app
from app.switcher import CurrentLocation, SwitchGate, choose_display, perform_switch

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


def test_exit_payload_keeps_city_and_country_apart() -> None:
    ipinfo = parse_exit_payload({"ip": "203.0.113.5", "city": "Amsterdam", "country": "NL"})
    assert (ipinfo.city, ipinfo.country, ipinfo.country_code) == ("Amsterdam", None, "NL")

    ip_api = parse_exit_payload(
        {
            "query": "203.0.113.8",
            "city": "Frankfurt",
            "regionName": "Hesse",
            "country": "Germany",
            "countryCode": "DE",
        }
    )
    assert ip_api.city == "Frankfurt"
    assert ip_api.country == "Germany"
    assert ip_api.country_code == "DE"
    assert ip_api.label == "Frankfurt, Hesse, Germany"

    # Gluetun's control server: public_ip, full country name, city as Gluetun spells it.
    gluetun = parse_exit_payload(
        {
            "public_ip": "79.115.174.19",
            "region": "Zurich",
            "country": "Switzerland",
            "city": "Zürich",
        }
    )
    assert gluetun.ip == "79.115.174.19"
    assert gluetun.city == "Zürich"
    assert gluetun.country == "Switzerland"
    assert gluetun.country_code is None


def test_control_server_is_preferred_and_sends_the_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"public_ip": "203.0.113.7", "city": "Zürich", "country": "Switzerland"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("app.ipcheck.httpx.Client", FakeClient)
    settings = _settings(tmp_path)
    settings = _with(settings, gluetun_control_url="http://gluetun:8000/", gluetun_control_api_key="top-secret")
    status = resolve_exit(settings)
    assert captured["url"] == "http://gluetun:8000/v1/publicip/ip"
    assert captured["headers"]["X-API-Key"] == "top-secret"
    assert status.city == "Zürich"
    assert status.country == "Switzerland"
    assert status.ip == "203.0.113.7"


def test_proxy_is_used_when_the_control_server_has_no_city(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _with(_settings(tmp_path), gluetun_control_url="http://gluetun:8000")
    monkeypatch.setattr("app.ipcheck._fetch_control", lambda s: ExitStatus(ip="203.0.113.1"))
    monkeypatch.setattr(
        "app.ipcheck.fetch_exit_info",
        lambda s, timeout=None: ExitStatus(ip="203.0.113.2", city="Amsterdam", country_code="NL", label="Amsterdam, NL"),
    )
    status = resolve_exit(settings)
    assert status.city == "Amsterdam"
    assert status.ip == "203.0.113.2"


def test_saved_location_wins_over_the_exit() -> None:
    locations = [Location("Netherlands", "Amsterdam", "Amsterdam", "Europe", "NL")]
    display = choose_display(
        CurrentLocation("Netherlands", "Amsterdam"),
        ExitStatus(ip="203.0.113.1", city="Frankfurt", country="Germany"),
        locations,
    )
    assert display.heading == "Amsterdam, Netherlands"
    assert display.auto_picked is False
    assert display.matched is True
    assert display.note is None


def test_empty_location_is_named_from_the_exit() -> None:
    locations = [Location("Switzerland", "Zurich", "Zurich", "Europe", "CH")]
    display = choose_display(
        CurrentLocation("", ""),
        ExitStatus(ip="203.0.113.1", city="Zürich", country="Switzerland", country_code="CH"),
        locations,
    )
    assert display.heading == "Zurich, Switzerland"
    assert display.note == "Auto-picked"
    assert display.auto_picked is True
    assert display.matched is True
    assert display.city == "Zurich"


def test_empty_location_says_unknown_when_the_city_cannot_be_read() -> None:
    with_ip = choose_display(CurrentLocation("", ""), ExitStatus(ip="203.0.113.1"), [])
    assert with_ip.heading == "Location unknown"
    assert with_ip.note is not None and "203.0.113.1" in with_ip.note
    assert with_ip.matched is False

    with_error = choose_display(
        CurrentLocation("", ""),
        ExitStatus(error="Exit IP was not checked because GLUETUN_PROXY_URL is not set."),
        [],
    )
    assert with_error.heading == "Location unknown"
    assert with_error.note is not None and "could not be read" in with_error.note
    assert "No location selected" not in with_error.heading


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

    def fail_if_called(settings: Settings, timeout: float | None = None) -> ExitStatus:
        raise AssertionError("the exit lookup runs only when no country or city is saved")

    monkeypatch.setattr("app.main.resolve_exit", fail_if_called)

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


def test_empty_env_shows_the_auto_picked_city(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    settings.env_path.write_text("# keep\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setattr("app.locations.fetch_status", lambda url, timeout: SAMPLE)
    monkeypatch.setattr("app.dockerctl.subprocess.run", lambda command, **kwargs: _empty_inspect(command))
    monkeypatch.setattr(
        "app.main.resolve_exit",
        lambda s, timeout=None: ExitStatus(
            ip="203.0.113.50",
            label="Amsterdam, NL",
            city="Amsterdam",
            country_code="NL",
        ),
    )

    app = create_app(settings)
    with TestClient(app) as client:
        login_page = client.get("/login")
        page = client.post(
            "/login",
            data={"csrf_token": _csrf(login_page.text), "password": "test-password"},
            follow_redirects=True,
        )
    assert page.status_code == 200
    assert "No location selected" not in page.text
    assert '<h1 id="place">Amsterdam, Netherlands</h1>' in page.text
    assert "Auto-picked" in page.text
    assert "Exit 203.0.113.50" in page.text
    assert 'aria-pressed="true"' in page.text.split('data-city="Amsterdam"', 1)[1][:500]
    assert 'aria-pressed="false"' in page.text.split('data-city="Frankfurt"', 1)[1][:500]

    # /status reports the same choice, so the script can mark the switch
    # if the first paint did not.
    with TestClient(app) as client:
        client.post(
            "/login",
            data={"csrf_token": _csrf(client.get("/login").text), "password": "test-password"},
        )
        status = client.get("/status")
    body = status.json()
    assert body["place"] == "Amsterdam, Netherlands"
    assert body["auto_picked"] is True
    assert body["matched"] is True
    assert body["city"] == "Amsterdam"
    assert body["exit_ip"] == "203.0.113.50"


def test_empty_env_says_unknown_when_the_exit_has_no_city(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    settings.env_path.write_text("# keep\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setattr("app.locations.fetch_status", lambda url, timeout: SAMPLE)
    monkeypatch.setattr("app.dockerctl.subprocess.run", lambda command, **kwargs: _empty_inspect(command))
    monkeypatch.setattr(
        "app.main.resolve_exit",
        lambda s, timeout=None: ExitStatus(error="Exit IP was not checked because GLUETUN_PROXY_URL is not set."),
    )

    app = create_app(settings)
    with TestClient(app) as client:
        login_page = client.get("/login")
        page = client.post(
            "/login",
            data={"csrf_token": _csrf(login_page.text), "password": "test-password"},
            follow_redirects=True,
        )
    assert "No location selected" not in page.text
    assert "Location unknown" in page.text
    assert "could not be read" in page.text
    assert 'aria-pressed="true"' not in page.text


def _empty_inspect(command: list[str]) -> subprocess.CompletedProcess:
    if "inspect" in command:
        fmt = command[command.index("--format") + 1]
        if "Health" in fmt:
            stdout = "healthy running\n"
        else:
            stdout = "SERVER_COUNTRIES=\nSERVER_CITIES=\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")
    return subprocess.CompletedProcess(command, 0, "", "")


def _with(settings: Settings, **changes: object) -> Settings:
    from dataclasses import replace

    return replace(settings, **changes)


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
