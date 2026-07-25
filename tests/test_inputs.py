"""Tests for synthetic input.

The Windows backend cannot be exercised off-Windows, so these tests drive the
recording backend and assert on the exact event sequence -- which is what
actually encodes the behaviour that matters (ordering, release-on-failure,
delta splitting).
"""

from __future__ import annotations

import pytest

from rappter_plays_palworld.inputs import (
    EXTENDED_KEYS,
    MAX_HOLD_SECONDS,
    MAX_MOUSE_DELTA,
    SCANCODES,
    InputError,
    Keyboard,
    RecordingInputBackend,
    resolve_scancode,
)


@pytest.fixture
def keyboard() -> Keyboard:
    return Keyboard(RecordingInputBackend())


def events(keyboard: Keyboard):
    return keyboard.backend.events


class TestScancodes:
    def test_wasd_have_the_canonical_set1_codes(self):
        # These specific values are why DirectInput games respond at all.
        assert SCANCODES["w"] == 0x11
        assert SCANCODES["a"] == 0x1E
        assert SCANCODES["s"] == 0x1F
        assert SCANCODES["d"] == 0x20

    def test_resolve_is_case_insensitive(self):
        assert resolve_scancode("W") == resolve_scancode("w")

    def test_resolve_strips_whitespace(self):
        assert resolve_scancode("  space ") == SCANCODES["space"]

    def test_unknown_key_is_a_clear_error(self):
        with pytest.raises(InputError, match="unknown key"):
            resolve_scancode("banana")

    def test_arrow_keys_are_marked_extended(self):
        for key in ("up", "down", "left", "right"):
            assert key in EXTENDED_KEYS


class TestTapAndHold:
    def test_tap_emits_down_then_up(self, keyboard):
        keyboard.tap("w")
        assert events(keyboard) == [("key_down", "w"), ("key_up", "w")]

    def test_hold_does_not_release(self, keyboard):
        keyboard.hold("w")
        assert events(keyboard) == [("key_down", "w")]
        assert keyboard.held == frozenset({"w"})

    def test_hold_is_idempotent(self, keyboard):
        keyboard.hold("w")
        keyboard.hold("w")
        assert events(keyboard) == [("key_down", "w")]

    def test_release_all_drops_every_held_key(self, keyboard):
        keyboard.hold("w")
        keyboard.hold("shift")
        keyboard.release_all()
        assert keyboard.held == frozenset()
        assert ("key_up", "w") in events(keyboard)
        assert ("key_up", "shift") in events(keyboard)

    def test_release_of_unheld_key_is_a_noop(self, keyboard):
        keyboard.release("w")
        assert events(keyboard) == []

    def test_tap_releases_even_if_sleep_is_interrupted(self, keyboard, monkeypatch):
        def boom(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr("rappter_plays_palworld.inputs._sleep", boom)
        with pytest.raises(KeyboardInterrupt):
            keyboard.tap("w", duration=1.0)
        # The key must not be left down.
        assert ("key_up", "w") in events(keyboard)

    def test_duration_is_clamped(self, keyboard, monkeypatch):
        recorded = []
        monkeypatch.setattr("rappter_plays_palworld.inputs._sleep", recorded.append)
        keyboard.tap("w", duration=9999)
        assert recorded == [MAX_HOLD_SECONDS]

    def test_negative_duration_becomes_zero(self, keyboard, monkeypatch):
        recorded = []
        monkeypatch.setattr("rappter_plays_palworld.inputs._sleep", recorded.append)
        keyboard.tap("w", duration=-5)
        assert recorded == [0.0]


class TestChord:
    def test_chord_presses_all_then_releases_in_reverse(self, keyboard):
        keyboard.chord(["shift", "w"])
        assert events(keyboard) == [
            ("key_down", "shift"),
            ("key_down", "w"),
            ("key_up", "w"),
            ("key_up", "shift"),
        ]

    def test_chord_releases_on_failure(self, keyboard, monkeypatch):
        def boom(_seconds):
            raise RuntimeError("interrupted")

        monkeypatch.setattr("rappter_plays_palworld.inputs._sleep", boom)
        with pytest.raises(RuntimeError):
            keyboard.chord(["shift", "w"])
        assert ("key_up", "w") in events(keyboard)
        assert ("key_up", "shift") in events(keyboard)


class TestMouse:
    def test_look_splits_into_steps(self, keyboard):
        keyboard.look(80, 0, steps=4, step_delay=0)
        moves = [event for event in events(keyboard) if event[0] == "mouse_move"]
        assert len(moves) == 4
        assert sum(move[1] for move in moves) == 80

    def test_look_remainder_is_not_lost(self, keyboard):
        # 10 does not divide evenly by 4; the total must still be exact.
        keyboard.look(10, 7, steps=4, step_delay=0)
        moves = [event for event in events(keyboard) if event[0] == "mouse_move"]
        assert sum(move[1] for move in moves) == 10
        assert sum(move[2] for move in moves) == 7

    def test_look_clamps_extreme_deltas(self, keyboard):
        keyboard.look(999999, 0, steps=2, step_delay=0)
        moves = [event for event in events(keyboard) if event[0] == "mouse_move"]
        assert sum(move[1] for move in moves) == MAX_MOUSE_DELTA

    def test_look_handles_negative_deltas(self, keyboard):
        keyboard.look(-100, -50, steps=5, step_delay=0)
        moves = [event for event in events(keyboard) if event[0] == "mouse_move"]
        assert sum(move[1] for move in moves) == -100
        assert sum(move[2] for move in moves) == -50

    def test_zero_look_emits_nothing(self, keyboard):
        keyboard.look(0, 0, steps=4, step_delay=0)
        assert events(keyboard) == []

    def test_click_is_down_then_up(self, keyboard):
        keyboard.click("right")
        assert events(keyboard) == [("mouse_down", "right"), ("mouse_up", "right")]

    def test_unknown_button_is_rejected(self, keyboard):
        with pytest.raises(InputError, match="unknown mouse button"):
            keyboard.click("scroll-wheel-of-doom")

    def test_click_releases_on_failure(self, keyboard, monkeypatch):
        def boom(_seconds):
            raise RuntimeError("interrupted")

        monkeypatch.setattr("rappter_plays_palworld.inputs._sleep", boom)
        with pytest.raises(RuntimeError):
            keyboard.click("left", duration=1.0)
        assert ("mouse_up", "left") in events(keyboard)

    def test_scroll(self, keyboard):
        keyboard.scroll(-2)
        assert events(keyboard) == [("scroll", -2)]


class TestBackendSelection:
    def test_recording_backend_is_available(self):
        assert RecordingInputBackend().available is True

    def test_recording_backend_validates_keys(self):
        backend = RecordingInputBackend()
        with pytest.raises(InputError):
            backend.key_down("not-a-key")
