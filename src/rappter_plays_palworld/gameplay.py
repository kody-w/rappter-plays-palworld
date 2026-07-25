"""The play loop: look at the screen, decide, press keys. Repeat.

This is the same loop that plays Pokemon Red, one rung harder. The differences
that actually matter:

*Pokemon Red is turn-based.* The emulator waits while the model thinks. Palworld
does not. The world keeps running during every decision, which forces two rules
this module enforces rather than hopes for:

1. **Held keys are released before thinking.** A decision takes seconds. If the
   agent is holding ``w`` when it stops to reason, the character runs into the
   sea. Movement is expressed as bounded, self-terminating bursts.
2. **Plans are short and capped.** A plan that would take longer than
   ``MAX_PLAN_SECONDS`` is truncated, because a stale plan executed against a
   changed world is worse than no plan.

*Pokemon Red has a RAM map.* Palworld has ``GET /game-data``, which is better in
some ways -- exact HP, position, level, and what every nearby actor is doing.
The frame is what the agent *sees*; the oracle is what it *knows*. Feeding both
stops the model inventing HP numbers off a blurry HUD.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .capture import CaptureError, Frame, ScreenCapture
from .inputs import InputError, Keyboard
from .profiles import GameProfile

LOGGER = logging.getLogger("openrappter.palworld.gameplay")

# A single plan may not occupy the character for longer than this. The world
# moves while the plan runs, so long plans act on stale information.
MAX_PLAN_SECONDS = 6.0
MAX_ACTIONS_PER_PLAN = 6

# One movement burst. Long enough to make progress, short enough that the agent
# re-checks the screen before walking off a cliff.
MAX_MOVE_SECONDS = 3.0


class PlanError(RuntimeError):
    """The model's plan could not be understood or executed."""


@dataclass
class Step:
    """One primitive the model may emit."""

    action: str
    duration: float = 0.0
    dx: int = 0
    dy: int = 0
    button: str = "left"
    seconds: float = 0.0
    sprint: bool = False

    @classmethod
    def parse(cls, payload: Any) -> "Step":
        if not isinstance(payload, dict):
            raise PlanError(f"step must be an object, got {type(payload).__name__}")
        action = str(payload.get("action") or "").strip().lower()
        if not action:
            raise PlanError("step is missing an 'action'")
        return cls(
            action=action,
            duration=_as_float(payload.get("duration"), 0.0),
            dx=_as_int(payload.get("dx"), 0),
            dy=_as_int(payload.get("dy"), 0),
            button=str(payload.get("button") or "left").strip().lower(),
            seconds=_as_float(payload.get("seconds"), 0.0),
            sprint=bool(payload.get("sprint")),
        )

    def estimated_seconds(self) -> float:
        if self.action == "wait":
            return max(0.0, self.seconds)
        if self.action == "look":
            return 0.1
        return max(0.05, self.duration)


@dataclass
class Plan:
    """What the model decided to do about one frame."""

    observation: str = ""
    reasoning: str = ""
    steps: tuple[Step, ...] = field(default_factory=tuple)
    say: Optional[str] = None
    error: str = ""

    @classmethod
    def parse(cls, text: str) -> "Plan":
        payload = _extract_json_object(text)
        if payload is None:
            return cls(error=f"model did not return JSON: {text[:200]}")

        raw_steps = payload.get("actions")
        if raw_steps is None:
            raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            return cls(error="plan has no 'actions' list")

        steps: list[Step] = []
        for item in raw_steps[:MAX_ACTIONS_PER_PLAN]:
            try:
                steps.append(Step.parse(item))
            except PlanError as error:
                LOGGER.debug("dropping malformed step: %s", error)

        say = payload.get("say")
        return cls(
            observation=str(payload.get("observation") or "").strip(),
            reasoning=str(payload.get("reasoning") or "").strip(),
            steps=tuple(steps),
            say=say.strip() if isinstance(say, str) and say.strip() else None,
        )

    def truncated_to_budget(self, budget: float = MAX_PLAN_SECONDS) -> "Plan":
        """Drop trailing steps that would push the plan past its time budget."""
        kept: list[Step] = []
        spent = 0.0
        for step in self.steps:
            cost = step.estimated_seconds()
            if kept and spent + cost > budget:
                break
            kept.append(step)
            spent += cost
        return Plan(
            observation=self.observation,
            reasoning=self.reasoning,
            steps=tuple(kept),
            say=self.say,
            error=self.error,
        )


class Executor:
    """Turns a Plan into real input, safely.

    Every failure path releases held keys. That is not defensive padding: an
    exception mid-plan while ``shift+w`` is held leaves the character sprinting
    until a human intervenes.
    """

    def __init__(self, profile: GameProfile, keyboard: Keyboard) -> None:
        self.profile = profile
        self.keyboard = keyboard

    def execute(self, plan: Plan) -> list[str]:
        performed: list[str] = []
        try:
            for step in plan.steps:
                performed.append(self._execute_step(step))
        finally:
            # Nothing stays held across the boundary of a plan.
            self.keyboard.release_all()
        return performed

    def _execute_step(self, step: Step) -> str:
        action = step.action

        if action == "wait":
            seconds = max(0.0, min(MAX_MOVE_SECONDS, step.seconds or step.duration))
            time.sleep(seconds)
            return f"wait {seconds:.2f}s"

        if action == "look":
            self.keyboard.look(step.dx, step.dy)
            return f"look dx={step.dx} dy={step.dy}"

        if action in ("click", "attack"):
            self.keyboard.click(step.button, duration=step.duration or 0.05)
            return f"click {step.button}"

        key = self.profile.key_for(action)
        if key is None:
            raise PlanError(
                f"action {action!r} is not in the {self.profile.name} profile; "
                f"valid actions: {', '.join(self.profile.actions)}"
            )

        # Movement actions are held for a bounded burst; everything else taps.
        if action.startswith("move_"):
            duration = max(0.05, min(MAX_MOVE_SECONDS, step.duration or 0.5))
            sprint_key = self.profile.key_for("sprint")
            if step.sprint and sprint_key:
                self.keyboard.chord([sprint_key, key], duration=duration)
                return f"{action} {duration:.2f}s (sprinting)"
            self.keyboard.tap(key, duration=duration)
            return f"{action} {duration:.2f}s"

        self.keyboard.tap(key, duration=step.duration or 0.05)
        return action


def build_system_prompt(profile: GameProfile) -> str:
    """Compose the model's instructions from the game profile."""
    lines = [
        f"You are RAPPter, an autonomous agent playing {profile.name}.",
        "",
        "You see the game exactly as a human player does: a screenshot of the "
        "live window. You act exactly as a human does: by pressing keys and "
        "moving the mouse. There is no API, no console, and no shortcut. If you "
        "cannot see it on screen, you do not know it.",
        "",
    ]

    if profile.real_time:
        lines += [
            "IMPORTANT: this game runs in real time. The world keeps moving "
            "while you think. Plan in short bursts of a few seconds, then look "
            "again. Never plan a long sequence blind.",
            "",
        ]

    lines += [f"Your goal: {profile.goal}", "", "Available actions:"]
    for action in profile.actions:
        note = profile.action_notes.get(action, "")
        lines.append(f"- {action}" + (f" -- {note}" if note else ""))
    lines += [
        "- look -- turn the camera. Provide dx and dy in mouse counts; "
        f"about {profile.look_step} is a moderate turn. Positive dx looks right.",
        "- click -- attack or use the held item. Provide button: left or right.",
        "- wait -- do nothing for a moment. Provide seconds.",
        "",
        "Movement actions accept a duration in seconds (max 3) and an optional "
        '"sprint": true.',
        "",
        f"Emit at most {MAX_ACTIONS_PER_PLAN} actions per turn, together lasting "
        f"no more than {MAX_PLAN_SECONDS:.0f} seconds.",
        "",
        "Respond with a single JSON object and nothing else:",
        '{"observation": "<what you see on screen right now>",',
        ' "reasoning": "<why you are about to do this>",',
        ' "actions": [{"action": "move_forward", "duration": 1.5}],',
        ' "say": "<optional short message to broadcast, or null>"}',
    ]
    return "\n".join(lines)


def build_turn_prompt(
    *,
    ground_truth: str = "",
    history: Sequence[str] = (),
    frame: Optional[Frame] = None,
) -> str:
    """Compose the per-turn text that accompanies the screenshot."""
    parts: list[str] = []

    if history:
        parts.append("What you did recently:\n" + "\n".join(history))

    if ground_truth:
        # The oracle is authoritative. Say so explicitly, or the model will
        # argue with it based on a misread HUD.
        parts.append(
            "Verified server state (authoritative -- trust this over the "
            "screenshot for numbers):\n" + ground_truth
        )

    if frame is not None and frame.age_seconds > 2.0:
        parts.append(
            f"NOTE: this screenshot is {frame.age_seconds:.1f}s old. "
            "Treat fast-moving details as stale."
        )

    parts.append("Look at the screenshot and decide your next few actions.")
    return "\n\n".join(parts)


@dataclass
class Turn:
    """One full cycle: frame in, plan out, actions performed."""

    index: int
    frame: Optional[Frame]
    plan: Plan
    performed: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""
    duration: float = 0.0

    def summary(self) -> str:
        if self.error:
            return f"[turn {self.index}] error: {self.error}"
        did = ", ".join(self.performed) if self.performed else "nothing"
        return f"[turn {self.index}] {self.plan.reasoning or '(no reasoning)'} -> {did}"


class PlayLoop:
    """Drives a game client through capture, decision, and input.

    Game-agnostic by construction: everything specific lives in the profile.
    """

    def __init__(
        self,
        profile: GameProfile,
        *,
        capture: Optional[ScreenCapture] = None,
        keyboard: Optional[Keyboard] = None,
        brain: Any = None,
        oracle: Any = None,
        dry_run: bool = False,
        history_turns: int = 5,
    ) -> None:
        self.profile = profile
        self.capture = capture or ScreenCapture(profile.window_title)
        self.keyboard = keyboard or Keyboard()
        self.executor = Executor(profile, self.keyboard)
        self.brain = brain
        self.oracle = oracle
        self.dry_run = dry_run
        self.history_turns = int(history_turns)

        self.system_prompt = build_system_prompt(profile)
        self._history: list[str] = []
        self._turn_index = 0

    def ground_truth(self) -> str:
        """Pull authoritative state from the oracle, if one is wired up."""
        if self.oracle is None:
            return ""
        try:
            return self.oracle.describe()
        except Exception as error:  # noqa: BLE001 - the oracle is optional
            LOGGER.debug("oracle unavailable: %s", error)
            return ""

    def step(self) -> Turn:
        """Run exactly one turn."""
        self._turn_index += 1
        started = time.monotonic()

        # Release everything before thinking. In a real-time game the character
        # would otherwise keep acting on the previous turn's intent.
        self.keyboard.release_all()

        frame: Optional[Frame] = None
        try:
            frame = self.capture.grab()
        except CaptureError as error:
            turn = Turn(
                index=self._turn_index,
                frame=None,
                plan=Plan(error=str(error)),
                error=str(error),
                duration=time.monotonic() - started,
            )
            self._remember(turn)
            return turn

        prompt = build_turn_prompt(
            ground_truth=self.ground_truth(),
            history=self._history,
            frame=frame,
        )

        if self.brain is None:
            plan = Plan(error="no brain configured")
        else:
            plan = self.brain.decide(
                system=self.system_prompt, prompt=prompt, frame=frame
            )

        plan = plan.truncated_to_budget()

        performed: list[str] = []
        error = plan.error
        if not error and not self.dry_run:
            try:
                performed = self.executor.execute(plan)
            except (PlanError, InputError) as exec_error:
                error = str(exec_error)
                self.keyboard.release_all()

        turn = Turn(
            index=self._turn_index,
            frame=frame,
            plan=plan,
            performed=tuple(performed),
            error=error,
            duration=time.monotonic() - started,
        )
        self._remember(turn)
        return turn

    def _remember(self, turn: Turn) -> None:
        self._history.append(turn.summary())
        del self._history[: -self.history_turns]

    def shutdown(self) -> None:
        """Always release input and free the capture handle."""
        self.keyboard.release_all()
        self.capture.close()


class RestOracle:
    """Ground-truth adapter over the Palworld REST API.

    Optional by design. The agent plays from the screen; this only sharpens the
    numbers it reasons about.
    """

    def __init__(self, client: Any, player_name: str = "") -> None:
        self.client = client
        self.player_name = player_name

    def describe(self) -> str:
        from .worldstate import summarise_actor

        snapshot = self.client.game_data()
        lines = [f"server fps {snapshot.fps:.0f}"]

        me = None
        if self.player_name:
            me = next(
                (
                    actor
                    for actor in snapshot.players
                    if actor.label.lower() == self.player_name.lower()
                ),
                None,
            )
        if me is None and snapshot.players:
            me = snapshot.players[0]

        if me is not None:
            lines.append("you: " + summarise_actor(me))
            nearby = _nearest(snapshot.actors, me, limit=8)
            if nearby:
                lines.append("nearby:")
                lines.extend(f"  {summarise_actor(actor)}" for actor in nearby)
        return "\n".join(lines)


def _nearest(actors, origin, *, limit: int = 8):
    def distance(actor) -> float:
        return sum(
            (a - b) ** 2 for a, b in zip(actor.location, origin.location, strict=False)
        )

    others = [actor for actor in actors if actor is not origin]
    return sorted(others, key=distance)[:limit]


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first balanced JSON object out of a model response."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return payload if isinstance(payload, dict) else None
    return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
