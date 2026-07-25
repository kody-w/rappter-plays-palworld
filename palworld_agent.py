"""A real OpenRappter agent that lets GitHub Copilot inhabit a Palworld server.

Palworld's official REST API is an exceptional sensor and a very limited
actuator. `GET /game-data` returns every actor in the world -- players, owned
pals, base pals, wild pals, NPCs -- with position, rotation, HP, guild,
ownership links, and the `Action`/`AI_Action` strings describing what each one
is doing. Nothing in the documented API moves a character, captures a pal, or
places a building.

So this agent is a warden, not a player. It watches the world continuously,
reasons about what changed, and speaks through `/announce`. It also holds the
administrative surface: save, kick, ban, unban, shutdown, stop.

In-world actuation requires a UE4SS server-side mod, which Pocketpair supports
only on the Windows dedicated server. That bridge is a separate deliverable;
this agent is designed so the actuator can be swapped in without reshaping the
decision loop -- see `Actuator` below.

Reference: https://docs.palworldgame.com/api/rest-api/palwold-rest-api
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from openrappter.agents.basic_agent import BasicAgent

from rappter_plays_palworld.gameplay import (
    Plan,
    PlayLoop,
    RestOracle,
)
from rappter_plays_palworld.inputs import IS_WINDOWS as IS_WINDOWS_HOST
from rappter_plays_palworld.profiles import get_profile
from rappter_plays_palworld.restapi import (
    PalworldApiError,
    PalworldAuthError,
    PalworldRestClient,
    PalworldUnavailableError,
    ServerMetrics,
    WorldSnapshot,
)
from rappter_plays_palworld.worldstate import (
    WorldDelta,
    build_digest,
    diff_snapshots,
)

LOGGER = logging.getLogger("openrappter.palworld")
MODULE_NAME = "openrappter.agents.palworld_agent"

DEFAULT_RUNTIME_DIR = Path.home() / ".openrappter" / "palworld"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "medium"

# `/game-data` serialises the whole world per call and Pocketpair publishes no
# rate limit, so poll conservatively by default.
DEFAULT_POLL_SECONDS = 10.0
MIN_POLL_SECONDS = 2.0

# Palworld's in-game chat is small. Anything longer gets truncated by the
# client, so the agent's voice is bounded here rather than server-side.
MAX_ANNOUNCE_CHARS = 200

DEFAULT_DECISION_TIMEOUT = 120.0

# How many past observations the model sees. Kept short: the digest is large
# and the useful signal is recent.
MEMORY_TURNS = 6

VALID_ACTIONS = (
    "play",
    "start",
    "status",
    "watch",
    "world",
    "players",
    "metrics",
    "announce",
    "save",
    "kick",
    "ban",
    "unban",
    "shutdown",
    "stop",
)

SYSTEM_PROMPT = """\
You are RAPPter, an autonomous warden living inside a self-hosted Palworld \
server. You perceive the world through periodic snapshots listing every actor \
present: players, their pals, base pals, wild pals and NPCs, each with \
position in metres, HP, level, guild, and what they are currently doing.

You cannot move, fight, build, or capture. Your only way to affect the world is \
to speak: a short in-game broadcast every player sees.

Your job is to be a genuinely useful presence:
- Notice what matters. A player dropping to low HP, a death, a newcomer \
arriving, someone levelling up, a guild forming.
- Speak rarely and well. Most ticks deserve silence. An announcement that adds \
nothing is worse than none.
- Be specific. Reference real names, real numbers, real events from the \
snapshot. Never invent something the data does not show.
- Keep any announcement under 200 characters.

Respond with a single JSON object and nothing else:
{"reasoning": "<one or two sentences of private thought>",
 "announce": "<message to broadcast, or null to stay silent>"}
"""


class Actuator:
    """Interface for anything that can change the world.

    The documented REST API has no world-mutating endpoint, so the default
    implementation is empty on purpose. A UE4SS server mod bridge can subclass
    this and expose move/interact/capture without the decision loop changing.
    """

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def capabilities(self) -> tuple[str, ...]:
        return ()

    def execute(self, command: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError(
            "No actuator is installed. In-world action requires a UE4SS "
            "server-side mod, which runs only on the Windows dedicated server."
        )


@dataclass
class Observation:
    """One perception tick."""

    at: str
    snapshot: WorldSnapshot
    metrics: Optional[ServerMetrics]
    delta: WorldDelta

    def digest(self, actor_limit: int) -> str:
        return build_digest(
            self.snapshot,
            metrics=self.metrics,
            delta=self.delta,
            actor_limit=actor_limit,
        )


@dataclass
class Decision:
    """What the model chose to do about an observation."""

    reasoning: str = ""
    announce: Optional[str] = None
    error: str = ""

    @classmethod
    def parse(cls, text: str) -> "Decision":
        payload = _extract_json_object(text)
        if payload is None:
            return cls(error=f"model did not return JSON: {text[:200]}")
        reasoning = str(payload.get("reasoning") or "").strip()
        raw = payload.get("announce")
        message: Optional[str] = None
        if isinstance(raw, str) and raw.strip() and raw.strip().lower() != "null":
            message = raw.strip()[:MAX_ANNOUNCE_CHARS]
        return cls(reasoning=reasoning, announce=message)


class Brain:
    """GitHub Copilot decision-maker.

    Imported lazily so the module stays importable (and testable) without the
    runtime extra installed.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        timeout: float = DEFAULT_DECISION_TIMEOUT,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from copilot import CopilotClient  # type: ignore
        except ImportError as error:  # pragma: no cover - runtime extra
            raise RuntimeError(
                "github-copilot-sdk is not installed; run ./bootstrap.sh --setup-only"
            ) from error
        # No tools, no skill discovery, no memory: the model sees only what
        # this agent hands it.
        self._client = CopilotClient(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            tools=[],
        )
        return self._client

    def decide(self, digest: str, history: Sequence[str]) -> Decision:
        prompt_parts = []
        if history:
            prompt_parts.append("Recent history:\n" + "\n".join(history))
        prompt_parts.append("Current world state:\n" + digest)
        prompt = "\n\n".join(prompt_parts)

        try:
            client = self._ensure_client()
            response = client.complete(
                system=SYSTEM_PROMPT,
                prompt=prompt,
                timeout=self.timeout,
            )
        except Exception as error:  # noqa: BLE001 - a bad tick must not kill the loop
            return Decision(error=f"decision failed: {error}")
        return Decision.parse(_response_text(response))


class VisionBrain(Brain):
    """Decides from a screenshot, the way a player does.

    This is the brain that actually plays. It receives the live frame as a PNG/
    JPEG attachment and returns a :class:`Plan` of key presses and mouse moves.
    The session carries no tools and no memory -- the model gets the frame, the
    ground-truth text, and nothing else.
    """

    def decide(self, *, system: str, prompt: str, frame: Any) -> Plan:  # type: ignore[override]
        try:
            client = self._ensure_client()
            response = client.complete(
                system=system,
                prompt=prompt,
                attachments=[
                    {
                        "media_type": getattr(frame, "media_type", "image/jpeg"),
                        "data": getattr(frame, "data", b""),
                    }
                ],
                timeout=self.timeout,
            )
        except Exception as error:  # noqa: BLE001 - one bad turn must not end the run
            return Plan(error=f"decision failed: {error}")
        return Plan.parse(_response_text(response))


class PlayRuntime:
    """Runs the play loop on a background thread with durable state."""

    def __init__(
        self,
        loop: PlayLoop,
        *,
        runtime_dir: Path = DEFAULT_RUNTIME_DIR,
        turn_delay: float = 0.0,
    ) -> None:
        self.loop = loop
        self.turn_delay = max(0.0, float(turn_delay))
        self.runtime_dir = Path(runtime_dir).expanduser()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.runtime_dir / "play.jsonl"

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._turns = 0
        self._errors = 0
        self._last_error = ""
        self._last_summary = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="palworld-play", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 20.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        # Releasing input on the way out is not optional: a half-executed plan
        # would otherwise leave the character running.
        self.loop.shutdown()

    def _run(self) -> None:
        LOGGER.info("play loop started (%s)", self.loop.profile.name)
        try:
            while not self._stop.is_set():
                try:
                    turn = self.loop.step()
                except Exception as error:  # noqa: BLE001 - keep playing
                    with self._lock:
                        self._errors += 1
                        self._last_error = str(error)
                    LOGGER.warning("turn failed: %s", error)
                    self._stop.wait(1.0)
                    continue

                with self._lock:
                    self._turns += 1
                    self._last_summary = turn.summary()
                    if turn.error:
                        self._errors += 1
                        self._last_error = turn.error

                self._record(turn)
                if self.turn_delay:
                    self._stop.wait(self.turn_delay)
        finally:
            self.loop.shutdown()
            LOGGER.info("play loop stopped")

    def _record(self, turn: Any) -> None:
        entry = {
            "turn": turn.index,
            "observation": turn.plan.observation,
            "reasoning": turn.plan.reasoning,
            "performed": list(turn.performed),
            "error": turn.error,
            "seconds": round(turn.duration, 2),
        }
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as error:  # pragma: no cover - disk issues
            LOGGER.warning("cannot write play log: %s", error)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "game": self.loop.profile.name,
                "turns": self._turns,
                "errors": self._errors,
                "last_error": self._last_error,
                "last_turn": self._last_summary,
                "dry_run": self.loop.dry_run,
                "input_backend": self.loop.keyboard.backend.name,
                "oracle": self.loop.oracle is not None,
            }


class WardenRuntime:
    """The perception/decision/speech loop, plus durable state."""

    def __init__(
        self,
        client: PalworldRestClient,
        *,
        runtime_dir: Path = DEFAULT_RUNTIME_DIR,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        actor_limit: int = 40,
        brain: Optional[Brain] = None,
        actuator: Optional[Actuator] = None,
        dry_run: bool = False,
    ) -> None:
        self.client = client
        self.runtime_dir = Path(runtime_dir).expanduser()
        self.poll_seconds = max(MIN_POLL_SECONDS, float(poll_seconds))
        self.actor_limit = int(actor_limit)
        self.brain = brain
        self.actuator = actuator or Actuator()
        self.dry_run = dry_run

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._previous: Optional[WorldSnapshot] = None
        self._history: list[str] = []
        self._events: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=512)
        self._ticks = 0
        self._announcements = 0
        self._errors = 0
        self._last_error = ""
        self._last_observation: Optional[Observation] = None
        self._last_decision: Optional[Decision] = None

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.runtime_dir / "state.json"
        self.log_path = self.runtime_dir / "events.jsonl"

    # ---- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="palworld-warden", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        self._persist()

    def _run(self) -> None:
        LOGGER.info("warden loop started (poll=%.1fs)", self.poll_seconds)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - keep the loop alive
                with self._lock:
                    self._errors += 1
                    self._last_error = str(error)
                LOGGER.warning("tick failed: %s", error)
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self.poll_seconds - elapsed))
        LOGGER.info("warden loop stopped")

    # ---- one iteration ---------------------------------------------------

    def observe(self) -> Observation:
        snapshot = self.client.game_data()
        try:
            metrics = self.client.metrics()
        except PalworldApiError:
            metrics = None
        delta = diff_snapshots(self._previous, snapshot)
        self._previous = snapshot
        return Observation(
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            snapshot=snapshot,
            metrics=metrics,
            delta=delta,
        )

    def tick(self) -> Observation:
        observation = self.observe()
        with self._lock:
            self._ticks += 1
            self._last_observation = observation

        # Silence is the default. Only spend a model call when the world
        # actually changed -- polling an idle server is most of the time.
        if not observation.delta:
            self._record({"kind": "tick", "at": observation.at, "changes": 0})
            return observation

        decision = Decision(reasoning="(no brain configured)")
        if self.brain is not None:
            decision = self.brain.decide(
                observation.digest(self.actor_limit), self._history
            )

        with self._lock:
            self._last_decision = decision
            if decision.error:
                self._errors += 1
                self._last_error = decision.error

        entry = {
            "kind": "decision",
            "at": observation.at,
            "changes": [event.render() for event in observation.delta.events],
            "reasoning": decision.reasoning,
            "announce": decision.announce,
        }

        if decision.announce:
            if self.dry_run:
                entry["announced"] = False
                entry["dry_run"] = True
            else:
                try:
                    self.client.announce(decision.announce)
                    entry["announced"] = True
                    with self._lock:
                        self._announcements += 1
                except PalworldApiError as error:
                    entry["announced"] = False
                    entry["announce_error"] = str(error)
                    with self._lock:
                        self._errors += 1
                        self._last_error = str(error)

        self._remember(observation, decision)
        self._record(entry)
        self._persist()
        return observation

    def _remember(self, observation: Observation, decision: Decision) -> None:
        summary = f"[{observation.at}] " + observation.delta.render().replace(
            "\n", "; "
        )
        if decision.announce:
            summary += f" | said: {decision.announce}"
        self._history.append(summary)
        del self._history[:-MEMORY_TURNS]

    # ---- persistence -----------------------------------------------------

    def _record(self, entry: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(entry)
        except queue.Full:
            pass
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as error:  # pragma: no cover - disk issues
            LOGGER.warning("cannot write event log: %s", error)

    def _persist(self) -> None:
        state = self.status()
        temporary = self.state_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(temporary, self.state_path)
        except OSError as error:  # pragma: no cover - disk issues
            LOGGER.warning("cannot persist state: %s", error)
        finally:
            temporary.unlink(missing_ok=True)

    # ---- introspection ---------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            observation = self._last_observation
            decision = self._last_decision
            payload: dict[str, Any] = {
                "running": self.running,
                "endpoint": self.client.base_url,
                "poll_seconds": self.poll_seconds,
                "ticks": self._ticks,
                "announcements": self._announcements,
                "errors": self._errors,
                "last_error": self._last_error,
                "dry_run": self.dry_run,
                "actuator": self.actuator.name,
                "actuator_available": self.actuator.available,
            }
            if observation is not None:
                payload["last_observation_at"] = observation.at
                payload["actors"] = len(observation.snapshot.actors)
                payload["players"] = len(observation.snapshot.players)
                payload["server_fps"] = observation.snapshot.fps
                payload["last_changes"] = [
                    event.render() for event in observation.delta.events
                ]
                if observation.metrics is not None:
                    payload["uptime_seconds"] = observation.metrics.uptime
                    payload["in_game_day"] = observation.metrics.days
            if decision is not None:
                payload["last_reasoning"] = decision.reasoning
                payload["last_announce"] = decision.announce
            return payload

    def drain_events(self, limit: int = 50) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while len(events) < limit:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events


# ---------------------------------------------------------------------------
# OpenRappter agent contract
# ---------------------------------------------------------------------------


class PalworldAgent(BasicAgent):
    def __init__(self):
        self.name = "Palworld"
        self.metadata = {
            "name": self.name,
            "description": (
                "Inhabit a self-hosted Palworld dedicated server through its "
                "official REST API. Continuously observes every actor in the "
                "world via game-data snapshots, reasons about what changed "
                "with GitHub Copilot, and speaks to players through in-game "
                "announcements. Also exposes the server administration "
                "surface: save, kick, ban, unban, shutdown, and stop."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(VALID_ACTIONS),
                        "description": "Warden lifecycle or server admin action",
                    },
                    "query": {
                        "type": "string",
                        "description": "Natural-language instruction",
                    },
                    "host": {
                        "type": "string",
                        "description": "Palworld server host or LAN IP",
                    },
                    "rest_port": {
                        "type": "integer",
                        "description": "REST API port (RESTAPIPort, commonly 8212)",
                    },
                    "password": {
                        "type": "string",
                        "description": (
                            "AdminPassword for REST Basic auth. Prefer the "
                            "PALWORLD_ADMIN_PASSWORD environment variable."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": "Message for the announce action",
                    },
                    "userid": {
                        "type": "string",
                        "description": (
                            "Target account id for kick/ban/unban, "
                            "e.g. steam_76561198000000000"
                        ),
                    },
                    "poll_seconds": {
                        "type": "number",
                        "description": "Seconds between world snapshots",
                    },
                    "waittime": {
                        "type": "integer",
                        "description": "Seconds to wait before shutdown",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Decide but send no input and broadcast nothing",
                    },
                    "game": {
                        "type": "string",
                        "description": "Game profile for play (default palworld)",
                    },
                    "player_name": {
                        "type": "string",
                        "description": "In-game character name, anchors ground truth",
                    },
                    "turn_delay": {
                        "type": "number",
                        "description": "Extra seconds to pause between play turns",
                    },
                },
                "required": ["action"],
            },
        }
        self._runtime: Optional[WardenRuntime] = None
        self._play_runtime: Optional[PlayRuntime] = None
        super().__init__(name=self.name, metadata=self.metadata)

    # ---- helpers ---------------------------------------------------------

    def _build_client(self, **kwargs: Any) -> PalworldRestClient:
        password = (
            kwargs.get("password") or os.environ.get("PALWORLD_ADMIN_PASSWORD") or ""
        )
        if not password:
            raise RuntimeError(
                "AdminPassword is required. Set PALWORLD_ADMIN_PASSWORD or pass "
                "password=... . It must match OptionSettings AdminPassword in "
                "PalWorldSettings.ini."
            )
        return PalworldRestClient(
            host=kwargs.get("host") or os.environ.get("PALWORLD_HOST") or "127.0.0.1",
            port=int(
                kwargs.get("rest_port") or os.environ.get("PALWORLD_REST_PORT") or 8212
            ),
            password=password,
        )

    def _require_runtime(self) -> WardenRuntime:
        if self._runtime is None:
            raise RuntimeError("The warden is not running. Use action='start' first.")
        return self._runtime

    def _client_for_oneshot(self, **kwargs: Any) -> PalworldRestClient:
        if self._runtime is not None:
            return self._runtime.client
        return self._build_client(**kwargs)

    # ---- entry point -----------------------------------------------------

    def perform(self, **kwargs):
        action = str(kwargs.pop("action", None) or "status").strip().lower()
        if action not in VALID_ACTIONS:
            return f"Unknown action '{action}'. Valid: {', '.join(VALID_ACTIONS)}"

        try:
            return self._dispatch(action, **kwargs)
        except PalworldAuthError as error:
            return f"Authentication failed: {error}"
        except PalworldUnavailableError as error:
            return (
                f"Cannot reach the server: {error}\n"
                "Check that the server is running and RESTAPIEnabled=True."
            )
        except (PalworldApiError, RuntimeError, ValueError) as error:
            return f"Error: {error}"

    def _dispatch(self, action: str, **kwargs: Any) -> str:
        if action == "play":
            return self._play(**kwargs)
        if action == "start":
            return self._start(**kwargs)
        if action == "stop":
            return self._stop_warden()
        if action == "status":
            return self._status()
        if action == "watch":
            return self._watch(**kwargs)

        client = self._client_for_oneshot(**kwargs)

        if action == "world":
            snapshot = client.game_data()
            try:
                metrics = client.metrics()
            except PalworldApiError:
                metrics = None
            return build_digest(snapshot, metrics=metrics, actor_limit=60)

        if action == "players":
            players = client.players()
            if not players:
                return "No players are connected."
            lines = [f"{len(players)} player(s) connected:"]
            lines.extend(
                f"- {p.name} ({p.userId}) Lv{p.level} "
                f"ping {p.ping:.0f}ms buildings {p.building_count}"
                for p in players
            )
            return "\n".join(lines)

        if action == "metrics":
            m = client.metrics()
            return (
                f"fps {m.serverfps} | frame {m.serverframetime:.1f}ms | "
                f"players {m.currentplayernum}/{m.maxplayernum} | "
                f"bases {m.basecampnum} | day {m.days} | "
                f"uptime {m.uptime // 3600}h{(m.uptime % 3600) // 60}m"
            )

        if action == "announce":
            message = str(kwargs.get("message") or kwargs.get("query") or "").strip()
            if not message:
                return "announce requires a message."
            client.announce(message[:MAX_ANNOUNCE_CHARS])
            return f"Announced: {message[:MAX_ANNOUNCE_CHARS]}"

        if action == "save":
            client.save()
            return "World saved."

        if action in ("kick", "ban", "unban"):
            userid = str(kwargs.get("userid") or "").strip()
            if not userid:
                return f"{action} requires a userid (e.g. steam_76561198000000000)."
            message = str(kwargs.get("message") or "").strip()
            if action == "kick":
                client.kick(userid, message)
                return f"Kicked {userid}."
            if action == "ban":
                client.ban(userid, message)
                return f"Banned {userid}."
            client.unban(userid)
            return f"Unbanned {userid}."

        if action == "shutdown":
            waittime = int(kwargs.get("waittime") or 30)
            message = str(kwargs.get("message") or "Server shutting down.").strip()
            client.shutdown(waittime, message)
            return f"Shutdown scheduled in {waittime}s."

        return f"Action '{action}' is not implemented."

    # ---- lifecycle actions ----------------------------------------------

    def _play(self, **kwargs: Any) -> str:
        """Play the game the way a person does: screen in, keystrokes out."""
        if self._play_runtime is not None and self._play_runtime.running:
            return "Already playing.\n" + self._status()

        profile = get_profile(str(kwargs.get("game") or "palworld"))

        # The oracle is optional. Without credentials the agent still plays --
        # it just reasons from pixels alone, with no ground-truth numbers.
        oracle = None
        try:
            client = self._build_client(**kwargs)
            if client.reachable():
                oracle = RestOracle(client, str(kwargs.get("player_name") or ""))
        except (RuntimeError, ValueError, PalworldApiError):
            oracle = None

        loop = PlayLoop(
            profile,
            brain=VisionBrain(
                model=str(kwargs.get("model") or DEFAULT_MODEL),
                reasoning_effort=str(
                    kwargs.get("reasoning_effort") or DEFAULT_REASONING_EFFORT
                ),
            ),
            oracle=oracle,
            dry_run=bool(kwargs.get("dry_run")),
        )

        # Fail loudly here rather than after the loop is already running: a
        # missing game window is the single most common setup mistake.
        try:
            rect = loop.capture.locate()
        except Exception as error:  # noqa: BLE001 - surfaced to the user verbatim
            loop.shutdown()
            return (
                f"Cannot capture the game: {error}\n"
                f"Make sure {profile.name} is running and visible on this desktop."
            )

        self._play_runtime = PlayRuntime(
            loop, turn_delay=float(kwargs.get("turn_delay") or 0.0)
        )
        self._play_runtime.start()

        backend = loop.keyboard.backend
        lines = [
            f"Playing {profile.name}.",
            f"  window   {rect.width}x{rect.height} at ({rect.left},{rect.top})",
            f"  input    {backend.name}"
            + (
                ""
                if backend.available
                else " (UNAVAILABLE -- no input will reach the game)"
            ),
            f"  oracle   {'connected' if oracle else 'none (pixels only)'}",
        ]
        if loop.dry_run:
            lines.append("  mode     dry run (decides but sends no input)")
        if not IS_WINDOWS_HOST:
            lines.append("")
            lines.append(
                "WARNING: this is not Windows. The Palworld client is "
                "Windows-only and synthetic input cannot reach it from here. "
                "Run the agent on the machine hosting the game."
            )
        return "\n".join(lines)

    def _start(self, **kwargs: Any) -> str:
        if self._runtime is not None and self._runtime.running:
            return "The warden is already running.\n" + self._status()

        client = self._build_client(**kwargs)
        info = client.info()

        brain = Brain(
            model=str(kwargs.get("model") or DEFAULT_MODEL),
            reasoning_effort=str(
                kwargs.get("reasoning_effort") or DEFAULT_REASONING_EFFORT
            ),
        )
        self._runtime = WardenRuntime(
            client,
            poll_seconds=float(kwargs.get("poll_seconds") or DEFAULT_POLL_SECONDS),
            brain=brain,
            dry_run=bool(kwargs.get("dry_run")),
        )
        self._runtime.start()
        return (
            f"Warden online.\n"
            f"  server   {info.servername or '(unnamed)'} v{info.version}\n"
            f"  world    {info.worldguid}\n"
            f"  endpoint {client.base_url}\n"
            f"  polling  every {self._runtime.poll_seconds:.0f}s"
            + (
                "\n  mode     dry run (will not broadcast)"
                if self._runtime.dry_run
                else ""
            )
        )

    def _stop_warden(self) -> str:
        stopped = []
        if self._play_runtime is not None and self._play_runtime.running:
            self._play_runtime.stop()
            stopped.append("play loop")
        if self._runtime is not None and self._runtime.running:
            self._runtime.stop()
            stopped.append("warden")
        if not stopped:
            return "Nothing is running."
        return "Stopped: " + ", ".join(stopped) + "."

    def _status(self) -> str:
        sections: list[str] = []

        if self._play_runtime is not None:
            play = self._play_runtime.status()
            lines = [
                f"playing     {play['game']} "
                f"({'running' if play['running'] else 'stopped'})",
                f"turns       {play['turns']} ({play['errors']} errors)",
                f"input       {play['input_backend']}",
                f"oracle      {'connected' if play['oracle'] else 'pixels only'}",
            ]
            if play["last_turn"]:
                lines.append(f"last turn   {play['last_turn']}")
            if play["last_error"]:
                lines.append(f"last error  {play['last_error']}")
            sections.append("\n".join(lines))

        if self._runtime is None:
            if sections:
                return "\n\n".join(sections)
            return (
                "Nothing is running. Use action='play' to play the game, "
                "or action='start' for the server warden."
            )
        state = self._runtime.status()
        lines = [
            f"warden      {'running' if state['running'] else 'stopped'}",
            f"endpoint    {state['endpoint']}",
            f"ticks       {state['ticks']} "
            f"({state['announcements']} announcements, {state['errors']} errors)",
        ]
        if "actors" in state:
            lines.append(
                f"world       {state['actors']} actors, "
                f"{state['players']} players, {state['server_fps']:.1f} fps"
            )
        if state.get("last_changes"):
            lines.append("recent      " + "; ".join(state["last_changes"][:5]))
        if state.get("last_announce"):
            lines.append(f"last said   {state['last_announce']}")
        if state.get("last_error"):
            lines.append(f"last error  {state['last_error']}")
        lines.append(
            f"actuator    {state['actuator']} "
            f"({'available' if state['actuator_available'] else 'none installed'})"
        )
        sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def _watch(self, **kwargs: Any) -> str:
        runtime = self._require_runtime()
        events = runtime.drain_events(int(kwargs.get("limit") or 20))
        if not events:
            return "No new events."
        return "\n".join(json.dumps(event) for event in events)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
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


def _response_text(response: Any) -> str:
    """Normalise whatever the Copilot SDK returned into text."""
    if isinstance(response, str):
        return response
    for attribute in ("text", "content", "output_text", "message"):
        value = getattr(response, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return str(response)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="palworld_agent",
        description="Run the Palworld warden agent against a dedicated server",
    )
    parser.add_argument("action", nargs="?", default="status", choices=VALID_ACTIONS)
    parser.add_argument("--host", default=os.environ.get("PALWORLD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--rest-port",
        type=int,
        default=int(os.environ.get("PALWORLD_REST_PORT", "8212")),
    )
    parser.add_argument("--password", default=os.environ.get("PALWORLD_ADMIN_PASSWORD"))
    parser.add_argument("--message")
    parser.add_argument("--userid")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--waittime", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--foreground", action="store_true", help="Block after start")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    agent = PalworldAgent()
    result = agent.perform(
        action=args.action,
        host=args.host,
        rest_port=args.rest_port,
        password=args.password,
        message=args.message,
        userid=args.userid,
        poll_seconds=args.poll_seconds,
        waittime=args.waittime,
        dry_run=args.dry_run,
    )
    print(result)

    if args.action == "start" and args.foreground:
        stop = threading.Event()

        def _handle(signum: int, frame: Any) -> None:  # noqa: ARG001
            stop.set()

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        print("\nWarden running. Press Ctrl-C to stop.\n")
        while not stop.is_set():
            stop.wait(1.0)
        print(agent.perform(action="stop"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
