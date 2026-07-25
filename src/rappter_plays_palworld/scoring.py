"""Objective scoring for an agent's run.

Most 3D game-agent evaluations are mushy because there is no ground truth: you
end up parsing pixels, or worse, trusting the agent's own narration of what it
accomplished. A model that is good at describing progress scores as well as a
model that makes progress.

Palworld avoids that. ``GET /game-data`` is server-authoritative and exact, so a
run can be scored on what actually happened in the world.

The hard rule this module exists to enforce: **the scorer never reads the
agent's reasoning, plans, or self-reports.** It only samples the oracle. If the
agent claims it captured six pals, the score reflects how many pals the server
says it owns. That independence is what makes the number mean anything.

Metrics are deliberately monotonic-where-possible and cheap to compute, so a run
can be scored live and compared across models and across sessions.
"""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .restapi import Actor, WorldSnapshot

# Palworld world coordinates are centimetres.
CM_PER_METRE = 100.0

# A single sample-to-sample jump larger than this is a teleport (fast travel,
# death respawn, or a missed sample), not walking. Counting it as distance
# travelled would reward dying repeatedly near a spawn point.
MAX_CREDIBLE_STEP_METRES = 150.0


@dataclass(frozen=True)
class Sample:
    """One observation of the scored player, taken from the oracle."""

    at: float
    level: int = 0
    hp: int = 0
    max_hp: int = 0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    pals_owned: int = 0
    guild_name: str = ""
    building_count: int = 0

    @property
    def alive(self) -> bool:
        """Derived, never stored.

        A stored ``alive`` flag can disagree with ``hp`` -- and a scorer whose
        death count contradicts its own HP readings is worthless.
        """
        return self.hp > 0

    @classmethod
    def from_snapshot(
        cls,
        snapshot: WorldSnapshot,
        *,
        at: float,
        player_name: str = "",
        building_count: int = 0,
    ) -> Optional["Sample"]:
        """Extract the scored player from a world snapshot.

        Returns None when the player is absent -- logged out, or the server
        dropped them -- which the run records as a gap rather than a zero.
        """
        player = _find_player(snapshot, player_name)
        if player is None:
            return None

        pals = sum(
            1
            for actor in snapshot.actors
            if actor.unit_type in ("OtomoPal", "BaseCampPal")
            and actor.trainer_instance_id == player.instance_id
        )
        return cls(
            at=at,
            level=player.level,
            hp=player.hp,
            max_hp=player.max_hp,
            position=player.location,
            pals_owned=pals,
            guild_name=player.guild_name,
            building_count=building_count,
        )


@dataclass
class ScoreCard:
    """The result of a scored run."""

    duration_seconds: float = 0.0
    samples: int = 0
    gaps: int = 0

    start_level: int = 0
    end_level: int = 0
    levels_gained: int = 0

    deaths: int = 0
    lowest_hp_fraction: float = 1.0
    time_alive_fraction: float = 1.0

    distance_metres: float = 0.0
    unique_regions: int = 0

    pals_gained: int = 0
    peak_pals: int = 0
    buildings_gained: int = 0
    joined_guild: bool = False

    milestones: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        minutes = self.duration_seconds / 60.0
        lines = [
            f"Run: {minutes:.1f} min, {self.samples} samples"
            + (f" ({self.gaps} gaps)" if self.gaps else ""),
            "",
            f"  levels      {self.start_level} -> {self.end_level} "
            f"(+{self.levels_gained})",
            f"  pals        +{self.pals_gained} (peak {self.peak_pals})",
            f"  buildings   +{self.buildings_gained}",
            f"  distance    {self.distance_metres:.0f} m "
            f"across {self.unique_regions} regions",
            f"  deaths      {self.deaths}",
            f"  survival    {self.time_alive_fraction * 100:.0f}% alive, "
            f"lowest HP {self.lowest_hp_fraction * 100:.0f}%",
            f"  guild       {'joined' if self.joined_guild else 'none'}",
        ]
        if self.milestones:
            lines += ["", "  milestones:"]
            lines += [f"    - {milestone}" for milestone in self.milestones]
        return "\n".join(lines)


class BenchmarkRun:
    """Accumulates samples and produces a ScoreCard.

    Sampling is decoupled from the play loop on purpose. The loop can stall,
    crash, or lie; the run keeps sampling the server regardless, so a crashed
    agent scores what it actually achieved before crashing.
    """

    def __init__(
        self,
        *,
        player_name: str = "",
        region_size_metres: float = 250.0,
    ) -> None:
        self.player_name = player_name
        self.region_size_metres = float(region_size_metres)
        self.samples: list[Sample] = []
        self.gaps = 0
        self._milestones: list[str] = []
        self._seen_milestones: set[str] = set()

    def observe(self, sample: Optional[Sample]) -> None:
        """Record one sample, or a gap when the player was not found."""
        if sample is None:
            self.gaps += 1
            return

        previous = self.samples[-1] if self.samples else None
        self.samples.append(sample)
        self._detect_milestones(previous, sample)

    def _detect_milestones(self, previous: Optional[Sample], current: Sample) -> None:
        def note(text: str) -> None:
            if text not in self._seen_milestones:
                self._seen_milestones.add(text)
                self._milestones.append(text)

        if previous is None:
            note(f"run began at level {current.level}")
            return

        # Level milestones at every multiple of 5 crossed.
        for threshold in range(previous.level + 1, current.level + 1):
            if threshold % 5 == 0:
                note(f"reached level {threshold}")

        if current.pals_owned > previous.pals_owned:
            note(f"owned {current.pals_owned} pals")

        if not previous.guild_name and current.guild_name:
            note(f"joined guild {current.guild_name}")

        if previous.alive and not current.alive:
            note("died")

        if current.building_count > previous.building_count == 0:
            note("placed first structure")

    def score(self) -> ScoreCard:
        if not self.samples:
            return ScoreCard(gaps=self.gaps)

        first, last = self.samples[0], self.samples[-1]

        deaths = sum(
            1
            for previous, current in _pairs(self.samples)
            if previous.alive and not current.alive
        )

        alive_samples = sum(1 for sample in self.samples if sample.alive)

        hp_fractions = [
            sample.hp / sample.max_hp for sample in self.samples if sample.max_hp > 0
        ]

        distance = 0.0
        for previous, current in _pairs(self.samples):
            step = _distance_metres(previous.position, current.position)
            # Fast travel and respawns are not locomotion.
            if step <= MAX_CREDIBLE_STEP_METRES:
                distance += step

        regions = {
            _region_key(sample.position, self.region_size_metres)
            for sample in self.samples
        }

        peak_pals = max(sample.pals_owned for sample in self.samples)

        return ScoreCard(
            duration_seconds=max(0.0, last.at - first.at),
            samples=len(self.samples),
            gaps=self.gaps,
            start_level=first.level,
            end_level=last.level,
            levels_gained=max(0, last.level - first.level),
            deaths=deaths,
            lowest_hp_fraction=min(hp_fractions) if hp_fractions else 1.0,
            time_alive_fraction=alive_samples / len(self.samples),
            distance_metres=distance,
            unique_regions=len(regions),
            pals_gained=max(0, last.pals_owned - first.pals_owned),
            peak_pals=peak_pals,
            buildings_gained=max(0, last.building_count - first.building_count),
            joined_guild=bool(last.guild_name) and not first.guild_name,
            milestones=tuple(self._milestones),
        )

    # ---- persistence -----------------------------------------------------

    def save(self, path: Path) -> Path:
        """Write the raw samples and the score card side by side.

        Raw samples are kept so a run can be re-scored later under a revised
        rubric without replaying the game.
        """
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "player_name": self.player_name,
            "gaps": self.gaps,
            "score": self.score().as_dict(),
            "samples": [asdict(sample) for sample in self.samples],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "BenchmarkRun":
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        run = cls(player_name=str(payload.get("player_name") or ""))
        run.gaps = int(payload.get("gaps") or 0)
        fields = {f.name for f in dataclasses.fields(Sample)}
        for raw in payload.get("samples") or []:
            # Drop anything not on the current Sample -- older runs may carry
            # fields that have since become derived properties.
            kept = {key: value for key, value in dict(raw).items() if key in fields}
            kept["position"] = tuple(kept.get("position") or (0.0, 0.0, 0.0))
            run.observe(Sample(**kept))
        return run


def compare(cards: Sequence[tuple[str, ScoreCard]]) -> str:
    """Render a side-by-side table of several runs.

    This is the point of the whole module: two models, same world, same rubric.
    """
    if not cards:
        return "no runs to compare"

    rows = [
        ("level +", lambda c: f"{c.levels_gained}"),
        ("pals +", lambda c: f"{c.pals_gained}"),
        ("buildings +", lambda c: f"{c.buildings_gained}"),
        ("distance m", lambda c: f"{c.distance_metres:.0f}"),
        ("regions", lambda c: f"{c.unique_regions}"),
        ("deaths", lambda c: f"{c.deaths}"),
        ("alive %", lambda c: f"{c.time_alive_fraction * 100:.0f}"),
        ("minutes", lambda c: f"{c.duration_seconds / 60:.1f}"),
    ]

    names = [name for name, _ in cards]
    width = max(12, max(len(name) for name in names) + 2)

    lines = ["metric".ljust(14) + "".join(name.ljust(width) for name in names)]
    lines.append("-" * (14 + width * len(names)))
    for label, getter in rows:
        lines.append(
            label.ljust(14) + "".join(getter(card).ljust(width) for _, card in cards)
        )
    return "\n".join(lines)


def _find_player(snapshot: WorldSnapshot, player_name: str) -> Optional[Actor]:
    players = snapshot.players
    if not players:
        return None
    if player_name:
        for actor in players:
            if actor.label.lower() == player_name.lower():
                return actor
        # Named player is absent; do not silently score somebody else.
        return None
    return players[0]


def _pairs(items: Sequence[Sample]) -> Iterable[tuple[Sample, Sample]]:
    return zip(items, items[1:], strict=False)


def _distance_metres(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> float:
    return (
        math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=False))) / CM_PER_METRE
    )


def _region_key(
    position: tuple[float, float, float], size_metres: float
) -> tuple[int, int]:
    size_cm = max(1.0, size_metres * CM_PER_METRE)
    return (int(position[0] // size_cm), int(position[1] // size_cm))
