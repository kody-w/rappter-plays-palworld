"""Tests for the Palworld REST API client."""

from __future__ import annotations

import base64
import json

import pytest

from rappter_plays_palworld.restapi import (
    Actor,
    PalworldApiError,
    PalworldAuthError,
    PalworldRestClient,
    PalworldUnavailableError,
    Player,
    ServerInfo,
    ServerMetrics,
    WorldSnapshot,
)


def make_client(**kwargs) -> PalworldRestClient:
    kwargs.setdefault("password", "a-sufficiently-long-secret")
    return PalworldRestClient(**kwargs)


class TestConstruction:
    def test_password_is_required(self):
        with pytest.raises(ValueError, match="AdminPassword is required"):
            PalworldRestClient(password="")

    def test_base_url_composition(self):
        client = make_client(host="10.0.0.5", port=9000)
        assert client.base_url == "http://10.0.0.5:9000/v1/api"

    def test_base_path_is_normalised(self):
        client = make_client(base_path="custom/api/")
        assert client.base_url.endswith("/custom/api")

    def test_basic_auth_header_uses_admin_username(self):
        client = make_client(password="hunter2hunter2")
        expected = base64.b64encode(b"admin:hunter2hunter2").decode()
        assert client._auth_header == f"Basic {expected}"


class TestParsing:
    def test_server_info(self, info_payload):
        info = ServerInfo.from_payload(info_payload)
        assert info.servername == "Palworld example Server"
        assert info.worldguid == "A7E97BAA767DB9029EF013BB71E993A0"

    def test_player(self, players_payload):
        player = Player.from_payload(players_payload["players"][0])
        assert player.userId == "steam_00000000000000000"
        assert player.playerId == "AFAFD830000000000000000000000000"
        assert player.ping == pytest.approx(3.14)
        assert player.building_count == 119

    def test_metrics(self, metrics_payload):
        metrics = ServerMetrics.from_payload(metrics_payload)
        assert metrics.serverfps == 57
        assert metrics.serverframetime == pytest.approx(16.7671)
        assert metrics.healthy is True

    def test_metrics_unhealthy_when_fps_is_zero(self):
        assert ServerMetrics.from_payload({"serverfps": 0}).healthy is False

    def test_world_snapshot(self, game_data_payload):
        snapshot = WorldSnapshot.from_payload(game_data_payload)
        assert snapshot.time == "2026-06-17 13:00:40"
        assert len(snapshot.actors) == 4
        assert len(snapshot.players) == 1
        assert len(snapshot.palboxes) == 1
        assert snapshot.players[0].label == "Kody"

    def test_is_active_parses_string_booleans(self):
        assert Actor.from_payload({"IsActive": "false"}).is_active is False
        assert Actor.from_payload({"IsActive": "true"}).is_active is True
        # Missing IsActive should not silently deactivate an actor.
        assert Actor.from_payload({}).is_active is True

    def test_malformed_fields_do_not_raise(self):
        actor = Actor.from_payload(
            {"level": "not-a-number", "HP": None, "LocationX": "x"}
        )
        assert actor.level == 0
        assert actor.hp == 0
        assert actor.location == (0.0, 0.0, 0.0)

    def test_actor_data_missing_yields_empty(self):
        assert WorldSnapshot.from_payload({}).actors == ()

    def test_pal_classification(self, character_factory):
        assert Actor.from_payload(character_factory("a", "OtomoPal")).is_pal
        assert Actor.from_payload(character_factory("b", "WildPal")).is_pal
        assert not Actor.from_payload(character_factory("c", "Player")).is_pal
        assert Actor.from_payload(character_factory("c", "Player")).is_player


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRequests:
    def test_get_players_round_trip(self, monkeypatch, players_payload):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["auth"] = request.headers.get("Authorization")
            return FakeResponse(json.dumps(players_payload).encode())

        monkeypatch.setattr(
            "rappter_plays_palworld.restapi.urllib.request.urlopen", fake_urlopen
        )
        players = make_client().players()

        assert captured["url"] == "http://127.0.0.1:8212/v1/api/players"
        assert captured["method"] == "GET"
        assert captured["auth"].startswith("Basic ")
        assert len(players) == 1
        assert players[0].name == "PalUser"

    def test_announce_posts_json_body(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["method"] = request.method
            captured["body"] = json.loads(request.data.decode())
            captured["content_type"] = request.headers.get("Content-type")
            return FakeResponse(b"")

        monkeypatch.setattr(
            "rappter_plays_palworld.restapi.urllib.request.urlopen", fake_urlopen
        )
        make_client().announce("hello world")

        assert captured["method"] == "POST"
        assert captured["body"] == {"message": "hello world"}
        assert captured["content_type"] == "application/json"

    def test_announce_rejects_empty_message(self):
        with pytest.raises(ValueError, match="non-empty message"):
            make_client().announce("   ")

    def test_kick_requires_userid(self):
        with pytest.raises(ValueError, match="requires a userid"):
            make_client().kick("")

    def test_shutdown_omits_blank_message(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse(b"")

        monkeypatch.setattr(
            "rappter_plays_palworld.restapi.urllib.request.urlopen", fake_urlopen
        )
        make_client().shutdown(60)
        assert captured["body"] == {"waittime": 60}

    def test_401_raises_auth_error(self, monkeypatch):
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr(
            "rappter_plays_palworld.restapi.urllib.request.urlopen", fake_urlopen
        )
        with pytest.raises(PalworldAuthError) as excinfo:
            make_client().info()
        assert excinfo.value.status == 401

    def test_connection_failure_raises_unavailable(self, monkeypatch):
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(
            "rappter_plays_palworld.restapi.urllib.request.urlopen", fake_urlopen
        )
        with pytest.raises(PalworldUnavailableError):
            make_client().info()

    def test_other_http_errors_raise_api_error(self, monkeypatch):
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "Boom", {}, None)

        monkeypatch.setattr(
            "rappter_plays_palworld.restapi.urllib.request.urlopen", fake_urlopen
        )
        with pytest.raises(PalworldApiError) as excinfo:
            make_client().metrics()
        assert excinfo.value.status == 500

    def test_reachable_is_false_when_api_errors(self, monkeypatch):
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("down")

        monkeypatch.setattr(
            "rappter_plays_palworld.restapi.urllib.request.urlopen", fake_urlopen
        )
        assert make_client().reachable() is False

    def test_non_json_body_returns_text(self, monkeypatch):
        monkeypatch.setattr(
            "rappter_plays_palworld.restapi.urllib.request.urlopen",
            lambda request, timeout=None: FakeResponse(b"OK"),
        )
        # save() discards the body but must not raise on a bare string reply.
        make_client().save()
