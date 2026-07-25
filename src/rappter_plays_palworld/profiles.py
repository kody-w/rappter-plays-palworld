"""Game profiles: everything that differs between one game and the next.

The play loop in ``gameplay.py`` is deliberately game-agnostic. It knows how to
look at a frame, ask a model what to do, and execute the answer. Everything
game-specific -- window title, keybinds, the vocabulary of actions the model is
allowed to emit, how to describe the goal -- lives here.

Adding a new game means adding a profile, not touching the loop. That is the
whole point: Pokemon Red proved the loop on a 2D emulator, Palworld is the same
loop against a real-time 3D client, and the next game should be a profile too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class GameProfile:
    """Describes one game to the agent."""

    name: str
    window_title: str
    # Maps a semantic action name the model uses ("move_forward") to the
    # physical key the game expects ("w"). The model never sees raw keys, so
    # rebinding a game means editing this table and nothing else.
    keybinds: Mapping[str, str]
    # Short natural-language description of each action, injected into the
    # prompt so the model knows its own vocabulary.
    action_notes: Mapping[str, str] = field(default_factory=dict)
    goal: str = ""
    # Whether the game keeps running while the model thinks. This changes the
    # loop's entire risk profile: a real-time game punishes slow decisions and
    # requires the agent to release held keys before it stalls.
    real_time: bool = True
    # Sensible bounds for one camera adjustment, in mouse counts.
    look_step: int = 200

    def key_for(self, action: str) -> str | None:
        return self.keybinds.get(action)

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(sorted(self.keybinds))


# Palworld's default keyboard layout. Verified against the in-game bindings
# screen; a player who has rebound their keys should edit this profile.
PALWORLD = GameProfile(
    name="Palworld",
    window_title="Palworld",
    keybinds={
        "move_forward": "w",
        "move_back": "s",
        "move_left": "a",
        "move_right": "d",
        "jump": "space",
        "sprint": "shift",
        "crouch": "c",
        "interact": "f",
        "inventory": "tab",
        "map": "m",
        "reload": "r",
        "command_pal": "q",
        "summon_pal": "e",
        "chat": "enter",
        "menu": "escape",
        "hotbar_1": "1",
        "hotbar_2": "2",
        "hotbar_3": "3",
        "hotbar_4": "4",
        "hotbar_5": "5",
    },
    action_notes={
        "interact": "Pick up items, open chests, talk to NPCs, mount a pal.",
        "command_pal": "Order your active pal to attack what you are aiming at.",
        "summon_pal": "Throw the pal sphere / summon the selected pal.",
        "inventory": "Opens inventory. Opening a menu stops movement input.",
        "map": "Opens the world map. Useful for orienting, but you are blind while it is open.",
    },
    goal=(
        "Survive and progress: gather resources, capture pals, build a base, "
        "and level up. Stay alive above all -- death costs your carried items."
    ),
    real_time=True,
    look_step=200,
)


# Kept as a worked example of the abstraction rather than a supported target:
# the Pokemon agent has its own repo and drives PyBoy directly, not a window.
POKEMON_RED = GameProfile(
    name="Pokemon Red",
    window_title="PyBoy",
    keybinds={
        "up": "up",
        "down": "down",
        "left": "left",
        "right": "right",
        "a": "z",
        "b": "x",
        "start": "enter",
        "select": "backspace",
    },
    goal="Reach the Hall of Fame.",
    real_time=False,
    look_step=0,
)


PROFILES: dict[str, GameProfile] = {
    "palworld": PALWORLD,
    "pokemon-red": POKEMON_RED,
}


def get_profile(name: str) -> GameProfile:
    key = str(name).strip().lower()
    if key not in PROFILES:
        raise KeyError(
            f"unknown game profile {name!r}; available: {', '.join(sorted(PROFILES))}"
        )
    return PROFILES[key]
