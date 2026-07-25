"""Tests for the play loop, plan parsing, and the executor's safety rules."""

from __future__ import annotations

import time

import pytest

from rappter_plays_palworld.capture import CaptureError, Frame
from rappter_plays_palworld.gameplay import (
    MAX_ACTIONS_PER_PLAN,
    MAX_MOVE_SECONDS,
    MAX_PLAN_SECONDS,
    Executor,
    Plan,
    PlanError,
    PlayLoop,
    RestOracle,
    Step,
    build_system_prompt,
    build_turn_prompt,
)
from rappter_plays_palworld.inputs import Keyboard, RecordingInputBackend
from rappter_plays_palworld.profiles import PALWORLD, POKEMON_RED, get_profile


@pytest.fixture
def keyboard() -> Keyboard:
    return Keyboard(RecordingInputBackend())


@pytest.fixture
def executor(keyboard) -> Executor:
    return Executor(PALWORLD, keyboard)


def frame(age: float = 0.0) -> Frame:
    return Frame(
        data=b"jpegbytes",
        width=1280,
        height=720,
        captured_at=time.monotonic() - age,
    )


class TestStepParsing:
    def test_minimal_step(self):
        step = Step.parse({"action": "move_forward"})
        assert step.action == "move_forward"

    def test_action_is_normalised(self):
        assert Step.parse({"action": "  MOVE_Forward "}).action == "move_forward"

    def test_missing_action_is_rejected(self):
        with pytest.raises(PlanError, match="missing an 'action'"):
            Step.parse({"duration": 1})

    def test_non_object_is_rejected(self):
        with pytest.raises(PlanError, match="must be an object"):
            Step.parse("move_forward")

    def test_bad_numbers_fall_back_to_defaults(self):
        step = Step.parse({"action": "look", "dx": "left", "duration": None})
        assert step.dx == 0
        assert step.duration == 0.0


class TestPlanParsing:
    def test_parses_a_full_plan(self):
        plan = Plan.parse(
            '{"observation": "a tree", "reasoning": "chop it", '
            '"actions": [{"action": "move_forward", "duration": 1.0}], '
            '"say": "hello"}'
        )
        assert plan.observation == "a tree"
        assert plan.reasoning == "chop it"
        assert len(plan.steps) == 1
        assert plan.say == "hello"
        assert not plan.error

    def test_accepts_steps_as_an_alias(self):
        plan = Plan.parse('{"steps": [{"action": "jump"}]}')
        assert len(plan.steps) == 1

    def test_prose_around_json_is_tolerated(self):
        plan = Plan.parse('Sure!\n{"actions": [{"action": "jump"}]}\nDone.')
        assert len(plan.steps) == 1

    def test_missing_actions_is_an_error(self):
        assert Plan.parse('{"reasoning": "hmm"}').error

    def test_non_json_is_an_error(self):
        assert Plan.parse("I will walk forward").error

    def test_malformed_steps_are_dropped_not_fatal(self):
        plan = Plan.parse(
            '{"actions": [{"action": "jump"}, {"nope": 1}, {"action": "crouch"}]}'
        )
        assert [step.action for step in plan.steps] == ["jump", "crouch"]

    def test_plan_length_is_capped(self):
        actions = ",".join(['{"action": "jump"}'] * 20)
        plan = Plan.parse('{"actions": [%s]}' % actions)
        assert len(plan.steps) == MAX_ACTIONS_PER_PLAN

    def test_null_say_is_none(self):
        assert Plan.parse('{"actions": [], "say": null}').say is None


class TestBudget:
    def test_long_plans_are_truncated(self):
        plan = Plan(
            steps=tuple(Step(action="move_forward", duration=3.0) for _ in range(5))
        )
        trimmed = plan.truncated_to_budget()
        total = sum(step.estimated_seconds() for step in trimmed.steps)
        assert total <= MAX_PLAN_SECONDS
        assert len(trimmed.steps) < 5

    def test_a_single_overlong_step_is_still_kept(self):
        # Better to run one clamped action than to do nothing at all.
        plan = Plan(steps=(Step(action="wait", seconds=999),))
        assert len(plan.truncated_to_budget().steps) == 1

    def test_short_plans_pass_through(self):
        plan = Plan(steps=(Step(action="jump"),))
        assert len(plan.truncated_to_budget().steps) == 1


class TestExecutor:
    def test_movement_maps_to_the_profile_key(self, executor, keyboard):
        executor.execute(Plan(steps=(Step(action="move_forward", duration=0.01),)))
        assert ("key_down", "w") in keyboard.backend.events

    def test_sprint_produces_a_chord(self, executor, keyboard):
        executor.execute(
            Plan(steps=(Step(action="move_forward", duration=0.01, sprint=True),))
        )
        downs = [e for e in keyboard.backend.events if e[0] == "key_down"]
        assert ("key_down", "shift") in downs
        assert ("key_down", "w") in downs

    def test_look_is_translated_to_mouse_motion(self, executor, keyboard):
        executor.execute(Plan(steps=(Step(action="look", dx=100),)))
        assert any(e[0] == "mouse_move" for e in keyboard.backend.events)

    def test_click(self, executor, keyboard):
        executor.execute(Plan(steps=(Step(action="click", button="right"),)))
        assert ("mouse_down", "right") in keyboard.backend.events

    def test_unknown_action_is_rejected_with_the_valid_list(self, executor):
        with pytest.raises(PlanError, match="not in the Palworld profile"):
            executor.execute(Plan(steps=(Step(action="cast_fireball"),)))

    def test_keys_are_released_even_when_a_step_fails(self, executor, keyboard):
        plan = Plan(
            steps=(
                Step(action="move_forward", duration=0.01),
                Step(action="cast_fireball"),
            )
        )
        with pytest.raises(PlanError):
            executor.execute(plan)
        assert keyboard.held == frozenset()

    def test_movement_duration_is_clamped(self, executor, keyboard, monkeypatch):
        recorded = []
        monkeypatch.setattr("rappter_plays_palworld.inputs._sleep", recorded.append)
        executor.execute(Plan(steps=(Step(action="move_forward", duration=999),)))
        assert max(recorded) <= MAX_MOVE_SECONDS

    def test_wait_sleeps(self, executor, monkeypatch):
        recorded = []
        monkeypatch.setattr(time, "sleep", recorded.append)
        executor.execute(Plan(steps=(Step(action="wait", seconds=0.5),)))
        assert recorded == [0.5]


class FakeCapture:
    def __init__(self, frames=None, error: str = ""):
        self._frames = list(frames or [])
        self._error = error
        self.closed = False

    def grab(self) -> Frame:
        if self._error:
            raise CaptureError(self._error)
        return self._frames.pop(0) if self._frames else frame()

    def close(self) -> None:
        self.closed = True


class ScriptedBrain:
    def __init__(self, plans):
        self._plans = list(plans)
        self.calls = []

    def decide(self, *, system, prompt, frame):
        self.calls.append({"system": system, "prompt": prompt, "frame": frame})
        return self._plans.pop(0) if self._plans else Plan(error="exhausted")


class TestPlayLoop:
    def test_one_turn_executes_the_plan(self, keyboard):
        brain = ScriptedBrain([Plan(steps=(Step(action="jump"),), reasoning="hop")])
        loop = PlayLoop(PALWORLD, capture=FakeCapture(), keyboard=keyboard, brain=brain)

        turn = loop.step()

        assert turn.index == 1
        assert turn.performed == ("jump",)
        assert not turn.error

    def test_dry_run_decides_but_does_not_press(self, keyboard):
        brain = ScriptedBrain([Plan(steps=(Step(action="jump"),))])
        loop = PlayLoop(
            PALWORLD,
            capture=FakeCapture(),
            keyboard=keyboard,
            brain=brain,
            dry_run=True,
        )

        turn = loop.step()

        assert brain.calls
        assert turn.performed == ()
        assert keyboard.backend.events == []

    def test_held_keys_are_released_before_thinking(self, keyboard):
        # A real-time game keeps running during the model call, so anything
        # held from the previous turn must be dropped first.
        keyboard.hold("w")
        brain = ScriptedBrain([Plan(steps=())])
        loop = PlayLoop(PALWORLD, capture=FakeCapture(), keyboard=keyboard, brain=brain)

        loop.step()

        assert ("key_up", "w") in keyboard.backend.events

    def test_capture_failure_becomes_a_turn_error(self, keyboard):
        loop = PlayLoop(
            PALWORLD,
            capture=FakeCapture(error="window not found"),
            keyboard=keyboard,
            brain=ScriptedBrain([]),
        )

        turn = loop.step()

        assert "window not found" in turn.error
        assert turn.frame is None

    def test_missing_brain_is_reported(self, keyboard):
        loop = PlayLoop(PALWORLD, capture=FakeCapture(), keyboard=keyboard)
        assert "no brain" in loop.step().error

    def test_history_is_bounded(self, keyboard):
        brain = ScriptedBrain([Plan(steps=(Step(action="jump"),)) for _ in range(10)])
        loop = PlayLoop(
            PALWORLD,
            capture=FakeCapture(),
            keyboard=keyboard,
            brain=brain,
            history_turns=3,
        )
        for _ in range(10):
            loop.step()
        assert len(loop._history) == 3

    def test_oracle_text_reaches_the_prompt(self, keyboard):
        class Oracle:
            def describe(self):
                return "you: Player Lv7 55/100hp"

        brain = ScriptedBrain([Plan(steps=())])
        loop = PlayLoop(
            PALWORLD,
            capture=FakeCapture(),
            keyboard=keyboard,
            brain=brain,
            oracle=Oracle(),
        )

        loop.step()

        assert "55/100hp" in brain.calls[0]["prompt"]
        assert "authoritative" in brain.calls[0]["prompt"]

    def test_a_broken_oracle_does_not_break_the_turn(self, keyboard):
        class Oracle:
            def describe(self):
                raise ConnectionError("server down")

        brain = ScriptedBrain([Plan(steps=(Step(action="jump"),))])
        loop = PlayLoop(
            PALWORLD,
            capture=FakeCapture(),
            keyboard=keyboard,
            brain=brain,
            oracle=Oracle(),
        )

        turn = loop.step()

        assert not turn.error
        assert turn.performed == ("jump",)

    def test_shutdown_releases_and_closes(self, keyboard):
        capture = FakeCapture()
        loop = PlayLoop(PALWORLD, capture=capture, keyboard=keyboard)
        keyboard.hold("w")

        loop.shutdown()

        assert keyboard.held == frozenset()
        assert capture.closed


class TestPrompts:
    def test_system_prompt_lists_profile_actions(self):
        prompt = build_system_prompt(PALWORLD)
        assert "move_forward" in prompt
        assert "command_pal" in prompt
        assert PALWORLD.goal in prompt

    def test_real_time_warning_only_for_real_time_games(self):
        assert "real time" in build_system_prompt(PALWORLD)
        assert "real time" not in build_system_prompt(POKEMON_RED)

    def test_stale_frame_is_flagged(self):
        prompt = build_turn_prompt(frame=frame(age=5.0))
        assert "5.0s old" in prompt

    def test_fresh_frame_is_not_flagged(self):
        assert "old" not in build_turn_prompt(frame=frame(age=0.1))


class TestProfiles:
    def test_lookup(self):
        assert get_profile("palworld") is PALWORLD
        assert get_profile("PALWORLD") is PALWORLD

    def test_unknown_profile_lists_the_options(self):
        with pytest.raises(KeyError, match="available"):
            get_profile("halo")

    def test_actions_are_sorted_and_complete(self):
        assert "move_forward" in PALWORLD.actions
        assert list(PALWORLD.actions) == sorted(PALWORLD.actions)

    def test_key_lookup(self):
        assert PALWORLD.key_for("move_forward") == "w"
        assert PALWORLD.key_for("nonsense") is None


class TestRestOracle:
    def test_describes_the_player_and_neighbours(self, game_data_payload):
        from rappter_plays_palworld.restapi import WorldSnapshot

        class Client:
            def game_data(self):
                return WorldSnapshot.from_payload(game_data_payload)

        text = RestOracle(Client(), player_name="Kody").describe()
        assert "you:" in text
        assert "Kody" in text
        assert "nearby:" in text
