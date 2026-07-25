"""Tests for the single-file agent.

OpenRappter is not a hard dependency of the test suite, so a minimal stub of
``openrappter.agents.basic_agent`` is installed before importing the agent.
That keeps the decision loop, the warden runtime and the dispatch table
testable in a bare checkout.
"""

from __future__ import annotations

import sys
import types

import pytest


def _install_openrappter_stub() -> None:
    if "openrappter.agents.basic_agent" in sys.modules:
        return

    class BasicAgent:
        def __init__(self, name: str = "", metadata: dict | None = None):
            self.name = name
            self.metadata = metadata or {}

    root = types.ModuleType("openrappter")
    agents = types.ModuleType("openrappter.agents")
    basic = types.ModuleType("openrappter.agents.basic_agent")
    basic.BasicAgent = BasicAgent
    agents.basic_agent = basic
    root.agents = agents

    sys.modules.setdefault("openrappter", root)
    sys.modules.setdefault("openrappter.agents", agents)
    sys.modules["openrappter.agents.basic_agent"] = basic


_install_openrappter_stub()

import palworld_agent  # noqa: E402
from palworld_agent import (  # noqa: E402
    MAX_ANNOUNCE_CHARS,
    Actuator,
    Decision,
    PalworldAgent,
    WardenRuntime,
    _extract_json_object,
    _response_text,
)
from rappter_plays_palworld.restapi import (  # noqa: E402
    ServerMetrics,
    WorldSnapshot,
)


class FakeClient:
    """Stands in for PalworldRestClient."""

    base_url = "http://fake:8212/v1/api"

    def __init__(self, snapshots: list[WorldSnapshot]):
        self._snapshots = list(snapshots)
        self.announced: list[str] = []
        self.saved = 0

    def game_data(self) -> WorldSnapshot:
        if not self._snapshots:
            raise AssertionError("FakeClient ran out of snapshots")
        return self._snapshots.pop(0)

    def metrics(self) -> ServerMetrics:
        return ServerMetrics(serverfps=60, currentplayernum=1, maxplayernum=32)

    def announce(self, message: str) -> None:
        self.announced.append(message)

    def save(self) -> None:
        self.saved += 1


class ScriptedBrain:
    def __init__(self, decisions: list[Decision]):
        self._decisions = list(decisions)
        self.calls = 0

    def decide(self, digest, history):
        self.calls += 1
        return self._decisions.pop(0) if self._decisions else Decision()


def snapshot(actors: list[dict]) -> WorldSnapshot:
    return WorldSnapshot.from_payload(
        {"Time": "2026-07-24 12:00:00", "FPS": 60.0, "ActorData": actors}
    )


class TestJsonExtraction:
    def test_plain_object(self):
        assert _extract_json_object('{"a": 1}') == {"a": 1}

    def test_object_wrapped_in_prose(self):
        text = 'Sure thing!\n{"announce": "hi"}\nHope that helps.'
        assert _extract_json_object(text) == {"announce": "hi"}

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        assert _extract_json_object('{"a": "} not the end {"}') == {
            "a": "} not the end {"
        }

    def test_escaped_quote_inside_string(self):
        assert _extract_json_object('{"a": "say \\"hi\\""}') == {"a": 'say "hi"'}

    def test_nested_objects(self):
        assert _extract_json_object('{"a": {"b": 2}}') == {"a": {"b": 2}}

    def test_returns_none_without_an_object(self):
        assert _extract_json_object("no json here") is None
        assert _extract_json_object("") is None

    def test_returns_none_for_malformed_json(self):
        assert _extract_json_object('{"a": }') is None

    def test_rejects_a_bare_array(self):
        assert _extract_json_object("[1, 2, 3]") is None


class TestDecision:
    def test_parses_announcement(self):
        decision = Decision.parse('{"reasoning": "r", "announce": "hello"}')
        assert decision.reasoning == "r"
        assert decision.announce == "hello"
        assert not decision.error

    def test_null_announce_means_silence(self):
        assert Decision.parse('{"announce": null}').announce is None

    def test_string_null_also_means_silence(self):
        assert Decision.parse('{"announce": "null"}').announce is None

    def test_blank_announce_means_silence(self):
        assert Decision.parse('{"announce": "   "}').announce is None

    def test_announcement_is_truncated(self):
        long = "x" * 500
        decision = Decision.parse('{"announce": "%s"}' % long)
        assert len(decision.announce) == MAX_ANNOUNCE_CHARS

    def test_non_json_becomes_an_error(self):
        decision = Decision.parse("I think we should say hello!")
        assert decision.error
        assert decision.announce is None


class TestResponseText:
    def test_plain_string(self):
        assert _response_text("hi") == "hi"

    def test_object_with_text_attribute(self):
        obj = types.SimpleNamespace(text="hello")
        assert _response_text(obj) == "hello"

    def test_falls_back_to_str(self):
        assert _response_text(42) == "42"


class TestActuator:
    def test_default_actuator_is_unavailable(self):
        actuator = Actuator()
        assert actuator.available is False
        assert actuator.capabilities() == ()

    def test_execute_explains_the_ue4ss_requirement(self):
        with pytest.raises(NotImplementedError, match="UE4SS"):
            Actuator().execute("move")


class TestWardenRuntime:
    def test_first_tick_reports_players_and_may_speak(
        self, tmp_path, character_factory
    ):
        client = FakeClient([snapshot([character_factory("P1", userid="s1")])])
        brain = ScriptedBrain([Decision(reasoning="hi", announce="Welcome!")])
        runtime = WardenRuntime(
            client, runtime_dir=tmp_path, brain=brain, poll_seconds=2
        )

        runtime.tick()

        assert brain.calls == 1
        assert client.announced == ["Welcome!"]

    def test_idle_tick_does_not_spend_a_model_call(self, tmp_path, character_factory):
        # Two identical snapshots: nothing changed, so the brain stays unused.
        state = [character_factory("P1", userid="s1")]
        client = FakeClient([snapshot(state), snapshot(state)])
        brain = ScriptedBrain([Decision(announce="first")])
        runtime = WardenRuntime(client, runtime_dir=tmp_path, brain=brain)

        runtime.tick()
        runtime.tick()

        assert brain.calls == 1
        assert client.announced == ["first"]

    def test_dry_run_never_broadcasts(self, tmp_path, character_factory):
        client = FakeClient([snapshot([character_factory("P1", userid="s1")])])
        brain = ScriptedBrain([Decision(announce="should not send")])
        runtime = WardenRuntime(client, runtime_dir=tmp_path, brain=brain, dry_run=True)

        runtime.tick()

        assert client.announced == []
        events = runtime.drain_events()
        assert any(event.get("dry_run") for event in events)

    def test_silence_is_respected(self, tmp_path, character_factory):
        client = FakeClient([snapshot([character_factory("P1", userid="s1")])])
        brain = ScriptedBrain([Decision(reasoning="nothing worth saying")])
        runtime = WardenRuntime(client, runtime_dir=tmp_path, brain=brain)

        runtime.tick()

        assert client.announced == []

    def test_brain_error_is_recorded_without_raising(self, tmp_path, character_factory):
        client = FakeClient([snapshot([character_factory("P1", userid="s1")])])
        brain = ScriptedBrain([Decision(error="model exploded")])
        runtime = WardenRuntime(client, runtime_dir=tmp_path, brain=brain)

        runtime.tick()

        status = runtime.status()
        assert status["errors"] == 1
        assert status["last_error"] == "model exploded"

    def test_poll_interval_has_a_floor(self, tmp_path):
        runtime = WardenRuntime(FakeClient([]), runtime_dir=tmp_path, poll_seconds=0.01)
        assert runtime.poll_seconds == palworld_agent.MIN_POLL_SECONDS

    def test_state_is_persisted(self, tmp_path, character_factory):
        client = FakeClient([snapshot([character_factory("P1", userid="s1")])])
        runtime = WardenRuntime(
            client,
            runtime_dir=tmp_path,
            brain=ScriptedBrain([Decision(announce="hello")]),
        )

        runtime.tick()

        assert runtime.state_path.is_file()
        assert runtime.log_path.is_file()

    def test_status_before_any_tick(self, tmp_path):
        runtime = WardenRuntime(FakeClient([]), runtime_dir=tmp_path)
        status = runtime.status()
        assert status["ticks"] == 0
        assert status["running"] is False
        assert status["actuator_available"] is False


class TestAgentContract:
    def test_metadata_shape(self):
        agent = PalworldAgent()
        assert agent.name == "Palworld"
        assert agent.metadata["name"] == "Palworld"
        assert "action" in agent.metadata["parameters"]["properties"]
        assert agent.metadata["parameters"]["required"] == ["action"]

    def test_unknown_action_is_rejected(self):
        result = PalworldAgent().perform(action="fly-to-the-moon")
        assert "Unknown action" in result

    def test_status_before_start(self):
        # Idle status points at both modes, so a new user learns 'play' exists.
        status = PalworldAgent().perform(action="status")
        assert "Nothing is running" in status
        assert "play" in status

    def test_stop_before_start(self):
        assert "Nothing is running" in PalworldAgent().perform(action="stop")

    def test_play_is_a_valid_action(self):
        assert (
            "play"
            in PalworldAgent().metadata["parameters"]["properties"]["action"]["enum"]
        )

    def test_missing_password_is_a_clear_error(self, monkeypatch):
        monkeypatch.delenv("PALWORLD_ADMIN_PASSWORD", raising=False)
        result = PalworldAgent().perform(action="start", host="127.0.0.1")
        assert "AdminPassword is required" in result

    def test_announce_requires_a_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PALWORLD_ADMIN_PASSWORD", "a-long-enough-secret")
        agent = PalworldAgent()
        agent._runtime = WardenRuntime(FakeClient([]), runtime_dir=tmp_path)
        result = agent.perform(action="announce")
        assert "requires a message" in result

    def test_kick_requires_a_userid(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PALWORLD_ADMIN_PASSWORD", "a-long-enough-secret")
        agent = PalworldAgent()
        agent._runtime = WardenRuntime(FakeClient([]), runtime_dir=tmp_path)
        assert "requires a userid" in agent.perform(action="kick")

    def test_save_delegates_to_the_client(self, tmp_path):
        agent = PalworldAgent()
        client = FakeClient([])
        agent._runtime = WardenRuntime(client, runtime_dir=tmp_path)
        assert agent.perform(action="save") == "World saved."
        assert client.saved == 1

    def test_announce_truncates(self, tmp_path):
        agent = PalworldAgent()
        client = FakeClient([])
        agent._runtime = WardenRuntime(client, runtime_dir=tmp_path)
        agent.perform(action="announce", message="y" * 500)
        assert len(client.announced[0]) == MAX_ANNOUNCE_CHARS
