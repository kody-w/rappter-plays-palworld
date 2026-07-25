"""Turn ``GET /game-data`` snapshots into agent-legible world state.

The Pokemon agent gets cheap structured state by reading emulator RAM. Palworld
has no emulator, but Pocketpair ships the equivalent for free: ``/game-data``
returns every actor in the world with position, rotation, HP, guild, ownership
links, and -- most usefully -- ``Action`` and ``AI_Action`` strings describing
what each actor is doing right now.

This module compresses that firehose into two things the model can actually
use: a bounded text digest, and a diff of what changed since the last tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .restapi import Actor, ServerMetrics, WorldSnapshot

# A busy server can hold hundreds of actors. Feeding all of them to the model
# every tick is wasteful and drowns the signal, so the digest keeps the most
# interesting ones: players first, then their active pals, then nearby wilds.
DEFAULT_DIGEST_ACTOR_LIMIT = 40

# Palworld world coordinates are centimetres. Divide for readable output.
CM_PER_METRE = 100.0


@dataclass(frozen=True)
class WorldEvent:
    """A single notable change between two snapshots."""

    kind: str
    subject: str
    detail: str = ""

    def render(self) -> str:
        return f"{self.kind}: {self.subject}" + (
            f" ({self.detail})" if self.detail else ""
        )


@dataclass(frozen=True)
class WorldDelta:
    """Everything that changed between two consecutive snapshots."""

    events: tuple[WorldEvent, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return bool(self.events)

    def render(self) -> str:
        if not self.events:
            return "no notable changes"
        return "\n".join(f"- {event.render()}" for event in self.events)


def actor_key(actor: Actor) -> str:
    """Stable identity for an actor across snapshots.

    Players keep a durable ``userid`` across reconnects; everything else is
    keyed on ``InstanceID``. Falling back to a composite avoids collapsing
    distinct unnamed wild pals into one key.
    """
    if actor.is_player and actor.userid:
        return f"player:{actor.userid}"
    if actor.instance_id:
        return f"actor:{actor.instance_id}"
    return f"anon:{actor.unit_type}:{actor.actor_class}:{actor.location}"


def index_actors(snapshot: WorldSnapshot) -> dict[str, Actor]:
    return {actor_key(actor): actor for actor in snapshot.actors}


def diff_snapshots(
    previous: WorldSnapshot | None,
    current: WorldSnapshot,
    *,
    hp_change_threshold: float = 0.25,
) -> WorldDelta:
    """Compare two snapshots and report the changes worth reacting to.

    Deliberately conservative: wild pals wander constantly, so movement alone
    is never an event. Only joins, departures, deaths, significant HP swings,
    guild changes and player level-ups surface.
    """
    if previous is None:
        events = [
            WorldEvent("online", actor.label, f"level {actor.level}")
            for actor in current.players
        ]
        return WorldDelta(tuple(events))

    before = index_actors(previous)
    after = index_actors(current)
    events: list[WorldEvent] = []

    for key, actor in after.items():
        if key in before:
            continue
        if actor.is_player:
            events.append(WorldEvent("joined", actor.label, f"level {actor.level}"))
        elif actor.unit_type in ("OtomoPal", "BaseCampPal"):
            owner = actor.trainer_nickname or "unknown trainer"
            events.append(WorldEvent("pal-appeared", actor.label, f"owned by {owner}"))

    for key, actor in before.items():
        if key in after:
            continue
        if actor.is_player:
            events.append(WorldEvent("left", actor.label))
        elif actor.unit_type in ("OtomoPal", "BaseCampPal"):
            owner = actor.trainer_nickname or "unknown trainer"
            events.append(WorldEvent("pal-gone", actor.label, f"was owned by {owner}"))

    for key, actor in after.items():
        prior = before.get(key)
        if prior is None:
            continue

        if prior.hp > 0 and actor.hp <= 0:
            events.append(WorldEvent("died", actor.label))
        elif actor.max_hp > 0 and prior.max_hp > 0:
            delta = (actor.hp - prior.hp) / float(actor.max_hp)
            if delta <= -hp_change_threshold:
                events.append(
                    WorldEvent(
                        "hurt",
                        actor.label,
                        f"{prior.hp} -> {actor.hp} / {actor.max_hp}",
                    )
                )

        if actor.is_player and actor.level > prior.level:
            events.append(
                WorldEvent("level-up", actor.label, f"{prior.level} -> {actor.level}")
            )

        if actor.guild_id != prior.guild_id:
            events.append(
                WorldEvent(
                    "guild-change",
                    actor.label,
                    f"{prior.guild_name or 'none'} -> {actor.guild_name or 'none'}",
                )
            )

    return WorldDelta(tuple(events))


def summarise_actor(actor: Actor) -> str:
    """One compact line describing an actor."""
    x, y, z = (value / CM_PER_METRE for value in actor.location)
    parts = [f"{actor.unit_type or actor.type}", actor.label]
    if actor.level:
        parts.append(f"Lv{actor.level}")
    if actor.max_hp:
        parts.append(f"{actor.hp}/{actor.max_hp}hp")
    if actor.guild_name:
        parts.append(f"guild={actor.guild_name}")
    if actor.trainer_nickname:
        parts.append(f"trainer={actor.trainer_nickname}")
    doing = actor.action or actor.ai_action
    if doing:
        parts.append(f"doing={doing}")
    parts.append(f"@({x:.0f},{y:.0f},{z:.0f})m")
    return " ".join(str(part) for part in parts if str(part))


def _digest_priority(actor: Actor) -> tuple[int, float]:
    """Sort key: players first, then owned pals, then everything else."""
    if actor.is_player:
        rank = 0
    elif actor.unit_type == "OtomoPal":
        rank = 1
    elif actor.unit_type == "BaseCampPal":
        rank = 2
    elif actor.unit_type == "NPC":
        rank = 3
    else:
        rank = 4
    # Within a rank, higher level is more interesting.
    return (rank, -float(actor.level))


def build_digest(
    snapshot: WorldSnapshot,
    *,
    metrics: ServerMetrics | None = None,
    delta: WorldDelta | None = None,
    actor_limit: int = DEFAULT_DIGEST_ACTOR_LIMIT,
) -> str:
    """Render a bounded, model-facing description of the world right now."""
    lines: list[str] = []

    header = f"World snapshot at {snapshot.time or 'unknown time'}"
    lines.append(header)
    lines.append(
        f"server fps {snapshot.fps:.1f} (avg {snapshot.average_fps:.1f})"
        + (
            f", {metrics.currentplayernum}/{metrics.maxplayernum} players"
            f", day {metrics.days}, {metrics.basecampnum} bases"
            f", uptime {metrics.uptime // 3600}h"
            if metrics
            else ""
        )
    )

    counts = tally_unit_types(snapshot.actors)
    if counts:
        lines.append(
            "actors: "
            + ", ".join(f"{name} x{count}" for name, count in sorted(counts.items()))
        )

    if delta is not None:
        lines.append("")
        lines.append("Changes since last tick:")
        lines.append(delta.render())

    ranked = sorted(snapshot.actors, key=_digest_priority)
    shown = ranked[: max(0, actor_limit)]
    if shown:
        lines.append("")
        lines.append(f"Actors ({len(shown)} of {len(snapshot.actors)}):")
        lines.extend(f"- {summarise_actor(actor)}" for actor in shown)
    if len(ranked) > len(shown):
        lines.append(f"... {len(ranked) - len(shown)} further actors omitted")

    return "\n".join(lines)


def tally_unit_types(actors: Iterable[Actor]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for actor in actors:
        name = actor.unit_type or actor.type or "Unknown"
        counts[name] = counts.get(name, 0) + 1
    return counts


def snapshot_from_mapping(payload: Mapping[str, object]) -> WorldSnapshot:
    """Convenience wrapper so callers can round-trip recorded JSON fixtures."""
    return WorldSnapshot.from_payload(payload)
