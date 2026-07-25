"""Tests for objective run scoring.

The property that matters most here is independence: the score must reflect the
oracle and nothing the agent said about itself. Several tests below exist purely
to pin down the anti-gaming rules (teleports don't count as distance, a missing
player doesn't score as a dead one, a named player is never confused with a
different one).
"""

from __future__ import annotations

import pytest

from rappter_plays_palworld.restapi import WorldSnapshot
from rappter_plays_palworld.scoring import (
    MAX_CREDIBLE_STEP_METRES,
    BenchmarkRun,
    Sample,
    ScoreCard,
    compare,
)


def snapshot(actors: list[dict]) -> WorldSnapshot:
    return WorldSnapshot.from_payload(
        {"Time": "2026-07-24 12:00:00", "FPS": 60.0, "ActorData": actors}
    )


def sample(at: float, **kwargs) -> Sample:
    kwargs.setdefault("level", 1)
    kwargs.setdefault("hp", 100)
    kwargs.setdefault("max_hp", 100)
    return Sample(at=at, **kwargs)


class TestSampleExtraction:
    def test_reads_the_named_player(self, character_factory):
        state = snapshot(
            [
                character_factory("P1", nickname="Kody", userid="s1", level=7),
                character_factory("P2", nickname="Other", userid="s2", level=99),
            ]
        )
        result = Sample.from_snapshot(state, at=0.0, player_name="Kody")
        assert result.level == 7

    def test_absent_named_player_returns_none(self, character_factory):
        # Critical: must not fall through and score a different player.
        state = snapshot([character_factory("P2", nickname="Other", userid="s2")])
        assert Sample.from_snapshot(state, at=0.0, player_name="Kody") is None

    def test_without_a_name_takes_the_only_player(self, character_factory):
        state = snapshot([character_factory("P1", nickname="Solo", userid="s1")])
        assert Sample.from_snapshot(state, at=0.0).level == 10

    def test_empty_world_returns_none(self):
        assert Sample.from_snapshot(snapshot([]), at=0.0) is None

    def test_counts_only_this_players_pals(self, character_factory):
        mine = character_factory("PAL1", "OtomoPal")
        mine["TrainerInstanceID"] = "P1"
        theirs = character_factory("PAL2", "OtomoPal")
        theirs["TrainerInstanceID"] = "P2"
        wild = character_factory("W1", "WildPal")

        state = snapshot(
            [character_factory("P1", nickname="Kody", userid="s1"), mine, theirs, wild]
        )
        result = Sample.from_snapshot(state, at=0.0, player_name="Kody")
        assert result.pals_owned == 1

    def test_zero_hp_is_not_alive(self, character_factory):
        state = snapshot([character_factory("P1", userid="s1", hp=0)])
        assert Sample.from_snapshot(state, at=0.0).alive is False


class TestScoring:
    def test_empty_run_scores_zero(self):
        card = BenchmarkRun().score()
        assert card.samples == 0
        assert card.levels_gained == 0

    def test_levels_gained(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, level=3))
        run.observe(sample(60.0, level=9))
        card = run.score()
        assert card.start_level == 3
        assert card.end_level == 9
        assert card.levels_gained == 6

    def test_level_loss_does_not_go_negative(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, level=9))
        run.observe(sample(60.0, level=3))
        assert run.score().levels_gained == 0

    def test_deaths_counted_on_transition(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, hp=100))
        run.observe(sample(1.0, hp=0))
        run.observe(sample(2.0, hp=0))
        run.observe(sample(3.0, hp=100))
        run.observe(sample(4.0, hp=0))
        # Two alive->dead transitions, not three dead samples.
        assert run.score().deaths == 2

    def test_time_alive_fraction(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, hp=100))
        run.observe(sample(1.0, hp=0))
        assert run.score().time_alive_fraction == pytest.approx(0.5)

    def test_lowest_hp_fraction(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, hp=100, max_hp=100))
        run.observe(sample(1.0, hp=25, max_hp=100))
        run.observe(sample(2.0, hp=90, max_hp=100))
        assert run.score().lowest_hp_fraction == pytest.approx(0.25)

    def test_distance_is_measured_in_metres(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, position=(0.0, 0.0, 0.0)))
        run.observe(sample(1.0, position=(1000.0, 0.0, 0.0)))  # 1000cm = 10m
        assert run.score().distance_metres == pytest.approx(10.0)

    def test_teleports_are_excluded_from_distance(self):
        # Fast travel and death respawns must not be rewarded as locomotion.
        run = BenchmarkRun()
        run.observe(sample(0.0, position=(0.0, 0.0, 0.0)))
        run.observe(sample(1.0, position=(10_000_000.0, 0.0, 0.0)))
        assert run.score().distance_metres == 0.0

    def test_a_step_just_under_the_threshold_counts(self):
        just_under = (MAX_CREDIBLE_STEP_METRES - 1) * 100.0
        run = BenchmarkRun()
        run.observe(sample(0.0, position=(0.0, 0.0, 0.0)))
        run.observe(sample(1.0, position=(just_under, 0.0, 0.0)))
        assert run.score().distance_metres > 0

    def test_unique_regions(self):
        run = BenchmarkRun(region_size_metres=100.0)
        run.observe(sample(0.0, position=(0.0, 0.0, 0.0)))
        run.observe(sample(1.0, position=(1000.0, 0.0, 0.0)))  # same 100m cell
        run.observe(sample(2.0, position=(50_000.0, 0.0, 0.0)))  # different cell
        assert run.score().unique_regions == 2

    def test_pals_and_buildings(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, pals_owned=0, building_count=0))
        run.observe(sample(1.0, pals_owned=5, building_count=12))
        run.observe(sample(2.0, pals_owned=3, building_count=12))
        card = run.score()
        assert card.pals_gained == 3
        assert card.peak_pals == 5
        assert card.buildings_gained == 12

    def test_guild_join_detected(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, guild_name=""))
        run.observe(sample(1.0, guild_name="Alpha"))
        assert run.score().joined_guild is True

    def test_already_in_a_guild_is_not_a_join(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, guild_name="Alpha"))
        run.observe(sample(1.0, guild_name="Alpha"))
        assert run.score().joined_guild is False

    def test_gaps_are_recorded_not_scored_as_death(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, hp=100))
        run.observe(None)
        run.observe(sample(2.0, hp=100))
        card = run.score()
        assert card.gaps == 1
        assert card.deaths == 0

    def test_duration(self):
        run = BenchmarkRun()
        run.observe(sample(100.0))
        run.observe(sample(400.0))
        assert run.score().duration_seconds == pytest.approx(300.0)


class TestMilestones:
    def test_run_start_is_recorded(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, level=3))
        assert any("run began" in m for m in run.score().milestones)

    def test_level_multiples_of_five(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, level=3))
        run.observe(sample(1.0, level=11))
        milestones = run.score().milestones
        assert any("reached level 5" in m for m in milestones)
        assert any("reached level 10" in m for m in milestones)

    def test_death_milestone(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, hp=100))
        run.observe(sample(1.0, hp=0))
        assert "died" in run.score().milestones

    def test_milestones_are_not_duplicated(self):
        run = BenchmarkRun()
        run.observe(sample(0.0, hp=100))
        run.observe(sample(1.0, hp=0))
        run.observe(sample(2.0, hp=100))
        run.observe(sample(3.0, hp=0))
        assert run.score().milestones.count("died") == 1


class TestPersistence:
    def test_round_trip(self, tmp_path):
        run = BenchmarkRun(player_name="Kody")
        run.observe(sample(0.0, level=3, position=(0.0, 0.0, 0.0)))
        run.observe(sample(60.0, level=8, position=(1000.0, 0.0, 0.0)))

        path = run.save(tmp_path / "run.json")
        restored = BenchmarkRun.load(path)

        assert restored.player_name == "Kody"
        assert restored.score().levels_gained == run.score().levels_gained
        assert restored.score().distance_metres == pytest.approx(
            run.score().distance_metres
        )

    def test_saved_payload_keeps_raw_samples_for_rescoring(self, tmp_path):
        import json

        run = BenchmarkRun()
        run.observe(sample(0.0))
        path = run.save(tmp_path / "run.json")
        payload = json.loads(path.read_text())
        assert payload["samples"]
        assert "score" in payload


class TestCompare:
    def test_renders_a_table(self):
        a = ScoreCard(levels_gained=5, deaths=1, distance_metres=1200.0)
        b = ScoreCard(levels_gained=2, deaths=4, distance_metres=300.0)
        table = compare([("opus", a), ("sonnet", b)])
        assert "opus" in table
        assert "sonnet" in table
        assert "level +" in table

    def test_empty(self):
        assert "no runs" in compare([])


class TestIndependence:
    def test_score_has_no_channel_for_agent_claims(self):
        # Guard against someone later adding a "the agent said it did X" input.
        # Scoring must depend only on oracle samples.
        run = BenchmarkRun()
        run.observe(sample(0.0, level=1))
        run.observe(sample(1.0, level=1))
        card = run.score()
        assert card.levels_gained == 0
        assert not hasattr(card, "reasoning")
        assert not hasattr(card, "claims")
