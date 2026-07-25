"""Tests for snapshot diffing and the model-facing digest."""

from __future__ import annotations

from rappter_plays_palworld.restapi import ServerMetrics, WorldSnapshot
from rappter_plays_palworld.worldstate import (
    actor_key,
    build_digest,
    diff_snapshots,
    summarise_actor,
    tally_unit_types,
)


def snapshot(actors: list[dict], time: str = "2026-07-24 10:00:00") -> WorldSnapshot:
    return WorldSnapshot.from_payload(
        {"Time": time, "FPS": 60.0, "AverageFPS": 58.0, "ActorData": actors}
    )


class TestActorKey:
    def test_players_key_on_userid(self, character_factory):
        first = snapshot([character_factory("P1", userid="steam_1")]).actors[0]
        # Same player, different InstanceID after a reconnect.
        second = snapshot([character_factory("P9", userid="steam_1")]).actors[0]
        assert actor_key(first) == actor_key(second)

    def test_non_players_key_on_instance_id(self, character_factory):
        actor = snapshot([character_factory("PAL1", "WildPal")]).actors[0]
        assert actor_key(actor) == "actor:PAL1"


class TestDiff:
    def test_first_snapshot_reports_online_players(self, character_factory):
        delta = diff_snapshots(None, snapshot([character_factory("P1", userid="s1")]))
        assert [event.kind for event in delta.events] == ["online"]

    def test_no_changes_is_falsy(self, character_factory):
        state = snapshot([character_factory("P1", userid="s1")])
        assert not diff_snapshots(state, state)

    def test_player_join_and_leave(self, character_factory):
        empty = snapshot([])
        one = snapshot([character_factory("P1", nickname="Kody", userid="s1")])

        joined = diff_snapshots(empty, one)
        assert [(e.kind, e.subject) for e in joined.events] == [("joined", "Kody")]

        left = diff_snapshots(one, empty)
        assert [(e.kind, e.subject) for e in left.events] == [("left", "Kody")]

    def test_significant_damage_is_reported(self, character_factory):
        before = snapshot([character_factory("P1", userid="s1", hp=100, max_hp=100)])
        after = snapshot([character_factory("P1", userid="s1", hp=40, max_hp=100)])
        kinds = [event.kind for event in diff_snapshots(before, after).events]
        assert "hurt" in kinds

    def test_minor_damage_is_ignored(self, character_factory):
        before = snapshot([character_factory("P1", userid="s1", hp=100, max_hp=100)])
        after = snapshot([character_factory("P1", userid="s1", hp=95, max_hp=100)])
        assert not diff_snapshots(before, after)

    def test_death_beats_damage(self, character_factory):
        before = snapshot([character_factory("P1", userid="s1", hp=100, max_hp=100)])
        after = snapshot([character_factory("P1", userid="s1", hp=0, max_hp=100)])
        kinds = [event.kind for event in diff_snapshots(before, after).events]
        assert kinds == ["died"]

    def test_level_up(self, character_factory):
        before = snapshot([character_factory("P1", userid="s1", level=5)])
        after = snapshot([character_factory("P1", userid="s1", level=6)])
        events = diff_snapshots(before, after).events
        assert events[0].kind == "level-up"
        assert "5 -> 6" in events[0].detail

    def test_guild_change(self, character_factory):
        before = snapshot(
            [character_factory("P1", userid="s1", guild_id="A", guild_name="Alpha")]
        )
        after = snapshot(
            [character_factory("P1", userid="s1", guild_id="B", guild_name="Beta")]
        )
        events = diff_snapshots(before, after).events
        assert events[0].kind == "guild-change"
        assert "Alpha -> Beta" in events[0].detail

    def test_wild_pal_movement_is_not_an_event(self, character_factory):
        before = snapshot([character_factory("W1", "WildPal", location=(0, 0, 0))])
        after = snapshot([character_factory("W1", "WildPal", location=(9999, 9999, 0))])
        assert not diff_snapshots(before, after)

    def test_wild_pals_appearing_are_not_events(self, character_factory):
        # Wild pals stream in and out constantly; only owned pals are notable.
        before = snapshot([])
        after = snapshot([character_factory("W1", "WildPal")])
        assert not diff_snapshots(before, after)

    def test_owned_pal_appearing_is_an_event(self, character_factory):
        before = snapshot([])
        after = snapshot([character_factory("PAL1", "OtomoPal")])
        assert [e.kind for e in diff_snapshots(before, after).events] == [
            "pal-appeared"
        ]


class TestDigest:
    def test_digest_includes_header_and_actors(self, game_data_payload):
        state = WorldSnapshot.from_payload(game_data_payload)
        digest = build_digest(
            state, metrics=ServerMetrics(currentplayernum=1, maxplayernum=32)
        )
        assert "World snapshot at 2026-06-17 13:00:40" in digest
        assert "Kody" in digest
        assert "1/32 players" in digest

    def test_digest_respects_actor_limit(self, character_factory):
        actors = [character_factory(f"W{i}", "WildPal") for i in range(50)]
        digest = build_digest(snapshot(actors), actor_limit=10)
        assert "Actors (10 of 50)" in digest
        assert "40 further actors omitted" in digest

    def test_players_are_ranked_before_wild_pals(self, character_factory):
        actors = [
            character_factory("W1", "WildPal", nickname="Chikipi"),
            character_factory("P1", "Player", nickname="Kody", userid="s1"),
        ]
        digest = build_digest(snapshot(actors), actor_limit=2)
        assert digest.index("Kody") < digest.index("Chikipi")

    def test_delta_section_appears_when_supplied(self, character_factory):
        state = snapshot([character_factory("P1", userid="s1")])
        digest = build_digest(state, delta=diff_snapshots(None, state))
        assert "Changes since last tick:" in digest

    def test_empty_delta_renders_readable_text(self, character_factory):
        state = snapshot([character_factory("P1", userid="s1")])
        assert diff_snapshots(state, state).render() == "no notable changes"

    def test_summarise_actor_converts_cm_to_metres(self, character_factory):
        actor = snapshot(
            [character_factory("P1", userid="s1", location=(10000.0, 20000.0, 300.0))]
        ).actors[0]
        assert "@(100,200,3)m" in summarise_actor(actor)

    def test_tally(self, game_data_payload):
        counts = tally_unit_types(WorldSnapshot.from_payload(game_data_payload).actors)
        assert counts["Player"] == 1
        assert counts["OtomoPal"] == 1
        assert counts["WildPal"] == 1
