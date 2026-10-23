"""Tests for turning AirVPN's status document into a sorted city list."""

from dataclasses import replace

import pytest

from app.config import Settings
from app.errors import LocationError
from app.locations import Location, LocationCache, gluetun_city, match_observed, parse_locations

SAMPLE = {
    "servers": [
        {
            "public_name": "One",
            "country_name": "Germany",
            "location": "Frankfurt",
            "continent": "Europe",
            "health": "ok",
        },
        {
            # Same city, second server. The list should keep a single row.
            "public_name": "Two",
            "country_name": "Germany",
            "location": "Frankfurt",
            "continent": "Europe",
            "health": "ok",
        },
        {
            "public_name": "Three",
            "country_name": "Netherlands",
            "location": "Alblasserdam",
            "continent": "Europe",
            "health": "ok",
        },
        {
            "public_name": "Four",
            "country_name": "United States",
            "location": "Phoenix, Arizona",
            "continent": "America",
            "health": "ok",
        },
        {
            "public_name": "Five",
            "country_name": "Sweden",
            "location": "Stockholm",
            "continent": "Europe",
            "health": "down",
        },
        {
            "public_name": "Six",
            "country_name": "Japan",
            "location": "Tokyo",
            "continent": "Asia",
            "health": "ok",
        },
    ]
}


def test_gluetun_strips_commas_the_way_its_updater_does() -> None:
    # Gluetun replaces ", " with a space, then deletes any remaining commas,
    # because SERVER_CITIES is itself comma-separated.
    assert gluetun_city("Zurich") == "Zurich"
    assert gluetun_city("New York City") == "New York City"
    assert gluetun_city("Phoenix, Arizona") == "Phoenix Arizona"
    assert gluetun_city("Phoenix,Arizona") == "PhoenixArizona"


def test_europe_filter_dedupes_and_sorts_country_then_city() -> None:
    locations = parse_locations(SAMPLE, frozenset({"europe"}))
    assert [(item.country, item.city, item.label) for item in locations] == [
        ("Germany", "Frankfurt", "Frankfurt"),
        ("Netherlands", "Alblasserdam", "Alblasserdam"),
    ]


def test_region_filter_is_case_insensitive_and_keeps_other_continents() -> None:
    locations = parse_locations(SAMPLE, frozenset({"america"}))
    assert len(locations) == 1
    assert locations[0].country == "United States"
    assert locations[0].city == "Phoenix Arizona"
    assert locations[0].label == "Phoenix, Arizona"


def test_empty_region_set_keeps_every_healthy_server() -> None:
    locations = parse_locations(SAMPLE, frozenset())
    countries = {item.country for item in locations}
    assert countries == {"Germany", "Netherlands", "United States", "Japan"}
    assert "Sweden" not in countries


def test_a_city_stays_if_any_server_there_is_healthy() -> None:
    payload = {
        "servers": [
            {"country_name": "Germany", "location": "Berlin", "continent": "Europe", "health": "down"},
            {"country_name": "Germany", "location": "Berlin", "continent": "Europe", "health": "ok"},
        ]
    }
    locations = parse_locations(payload, frozenset({"europe"}))
    assert [item.city for item in locations] == ["Berlin"]


def test_country_code_is_kept_for_matching() -> None:
    payload = {
        "servers": [
            {
                "country_name": "Netherlands",
                "country_code": "nl",
                "location": "Amsterdam",
                "continent": "Europe",
                "health": "ok",
            }
        ]
    }
    locations = parse_locations(payload, frozenset())
    assert locations[0].country_code == "NL"


def test_observed_exit_matches_a_list_row() -> None:
    locations = [
        Location("Netherlands", "Amsterdam", "Amsterdam", "Europe", "NL"),
        Location("Switzerland", "Zurich", "Zurich", "Europe", "CH"),
        Location("United States", "Phoenix Arizona", "Phoenix, Arizona", "America", "US"),
        Location("United States", "Portland", "Portland", "America", "US"),
        Location("United Kingdom", "Portland", "Portland", "Europe", "GB"),
    ]
    # ipinfo.io sends the country as a code. One Amsterdam in the list.
    assert match_observed(locations, "Amsterdam", "NL").city == "Amsterdam"
    # Gluetun spells the city with an umlaut; AirVPN does not.
    zurich = match_observed(locations, "Zürich", "Switzerland")
    assert zurich is not None and zurich.city == "Zurich"
    # The comma form is Gluetun's "Phoenix Arizona".
    phoenix = match_observed(locations, "Phoenix, Arizona", "US", "US")
    assert phoenix is not None and phoenix.city == "Phoenix Arizona"
    # Two Portlands. No country, so do not guess. A country picks one.
    assert match_observed(locations, "Portland", None) is None
    portland = match_observed(locations, "Portland", "United States")
    assert portland is not None and portland.country_code == "US"
    # The city exists, but the country disagrees.
    assert match_observed(locations, "Amsterdam", "Germany") is None
    assert match_observed(locations, "Berlin", "Germany") is None


def test_missing_servers_list_is_an_error() -> None:
    with pytest.raises(LocationError):
        parse_locations({"nope": []}, frozenset())


def test_cache_calls_the_api_once_until_it_expires(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls = {"n": 0}

    def fake(url: str, timeout: float) -> dict:
        calls["n"] += 1
        return SAMPLE

    monkeypatch.setattr("app.locations.fetch_status", fake)
    settings = _settings(tmp_path)
    cache = LocationCache(settings)
    assert cache.get() == cache.get()
    assert calls["n"] == 1


def test_cache_serves_the_previous_list_when_a_refresh_fails(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls = {"n": 0}

    def fake(url: str, timeout: float) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return SAMPLE
        raise LocationError("down")

    monkeypatch.setattr("app.locations.fetch_status", fake)
    # TTL of zero means the next call is already expired, so it tries again.
    settings = replace(_settings(tmp_path), location_cache_seconds=0)
    cache = LocationCache(settings)
    first = cache.get()
    second = cache.get()
    third = cache.get()
    assert second == first == third
    assert calls["n"] == 2


def test_cache_raises_when_it_has_nothing_to_fall_back_on(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    def fake(url: str, timeout: float) -> dict:
        raise LocationError("down")

    monkeypatch.setattr("app.locations.fetch_status", fake)
    cache = LocationCache(_settings(tmp_path))
    with pytest.raises(LocationError):
        cache.get()


def _settings(tmp_path) -> Settings:
    return Settings(
        password_hash="$2b$" + "x" * 50,
        session_secret="test-session-secret-value",
        stack_dir=tmp_path,
        regions=frozenset({"europe"}),
    )
