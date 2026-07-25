"""Synthetic keyboard and mouse input for a real game client.

The agent plays through the same interface a person uses, so this module has to
produce input the game cannot distinguish from a human's.

Two details matter more than anything else here:

1. **Scancodes, not virtual keys.** Palworld is an Unreal Engine DirectX title.
   DirectInput-style games read hardware scancodes and routinely ignore
   ``SendInput`` events that carry only a virtual-key code. Sending ``VK_W``
   does nothing; sending scancode ``0x11`` with ``KEYEVENTF_SCANCODE`` walks
   forward. Every key press here is scancode-based.

2. **Relative mouse motion.** A 3D camera is driven by mouse *deltas*, not
   screen coordinates. Warping the cursor to an absolute position does not turn
   the camera. ``MOUSEEVENTF_MOVE`` without ``MOUSEEVENTF_ABSOLUTE`` is the only
   thing that works.

The module imports cleanly on macOS and Linux -- the Windows backend simply
reports itself unavailable -- so the loop stays testable off-Windows.
"""

from __future__ import annotations

import ctypes
import platform
import time
from dataclasses import dataclass
from typing import Iterable

IS_WINDOWS = platform.system() == "Windows"

# DirectInput scancodes (US layout, set 1). These are hardware positions, not
# characters -- which is why they survive DirectInput's virtual-key filtering.
SCANCODES: dict[str, int] = {
    "escape": 0x01,
    "1": 0x02,
    "2": 0x03,
    "3": 0x04,
    "4": 0x05,
    "5": 0x06,
    "6": 0x07,
    "7": 0x08,
    "8": 0x09,
    "9": 0x0A,
    "0": 0x0B,
    "minus": 0x0C,
    "equals": 0x0D,
    "backspace": 0x0E,
    "tab": 0x0F,
    "q": 0x10,
    "w": 0x11,
    "e": 0x12,
    "r": 0x13,
    "t": 0x14,
    "y": 0x15,
    "u": 0x16,
    "i": 0x17,
    "o": 0x18,
    "p": 0x19,
    "enter": 0x1C,
    "ctrl": 0x1D,
    "a": 0x1E,
    "s": 0x1F,
    "d": 0x20,
    "f": 0x21,
    "g": 0x22,
    "h": 0x23,
    "j": 0x24,
    "k": 0x25,
    "l": 0x26,
    "shift": 0x2A,
    "z": 0x2C,
    "x": 0x2D,
    "c": 0x2E,
    "v": 0x2F,
    "b": 0x30,
    "n": 0x31,
    "m": 0x32,
    "alt": 0x38,
    "space": 0x39,
    "f1": 0x3B,
    "f2": 0x3C,
    "f3": 0x3D,
    "f4": 0x3E,
    "f5": 0x3F,
    "up": 0xC8,
    "left": 0xCB,
    "right": 0xCD,
    "down": 0xD0,
}

# Keys whose scancodes live in the extended set and need the E0 prefix flag.
EXTENDED_KEYS = frozenset({"up", "down", "left", "right"})

MOUSE_BUTTONS = ("left", "right", "middle")

# Win32 constants
_INPUT_KEYBOARD = 1
_INPUT_MOUSE = 0
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008
_KEYEVENTF_EXTENDEDKEY = 0x0001
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800

_BUTTON_FLAGS = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}

# Guard rails. A model that emits "hold w for 600 seconds" should not be able to
# strand the character running into a wall until someone notices.
MAX_HOLD_SECONDS = 10.0
MAX_MOUSE_DELTA = 4000


class InputError(RuntimeError):
    """The requested input could not be synthesised."""


if IS_WINDOWS:  # pragma: no cover - exercised only on the game host
    _ULONG_PTR = (
        ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
    )

    class _KeyBdInput(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(_ULONG_PTR)),
        ]

    class _MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(_ULONG_PTR)),
        ]

    class _InputUnion(ctypes.Union):
        _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput)]

    class _Input(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]

    def _send(structure: "_Input") -> None:
        sent = ctypes.windll.user32.SendInput(
            1, ctypes.byref(structure), ctypes.sizeof(_Input)
        )
        if sent != 1:
            raise InputError(
                f"SendInput was rejected (error {ctypes.windll.kernel32.GetLastError()}). "
                "If the game runs elevated, the agent must run elevated too."
            )


@dataclass(frozen=True)
class KeyPress:
    """One timed key press."""

    key: str
    duration: float = 0.05


class InputBackend:
    """Base class so tests and dry runs can stand in for real input."""

    name = "base"

    @property
    def available(self) -> bool:
        return False

    def key_down(self, key: str) -> None:
        raise NotImplementedError

    def key_up(self, key: str) -> None:
        raise NotImplementedError

    def mouse_move(self, dx: int, dy: int) -> None:
        raise NotImplementedError

    def mouse_down(self, button: str) -> None:
        raise NotImplementedError

    def mouse_up(self, button: str) -> None:
        raise NotImplementedError

    def scroll(self, clicks: int) -> None:
        raise NotImplementedError


class WindowsInputBackend(InputBackend):
    """Scancode-based SendInput. The only backend a real game accepts."""

    name = "windows"

    @property
    def available(self) -> bool:
        return IS_WINDOWS

    def _key_event(self, key: str, *, up: bool) -> None:  # pragma: no cover
        scancode = resolve_scancode(key)
        flags = _KEYEVENTF_SCANCODE
        if key in EXTENDED_KEYS:
            flags |= _KEYEVENTF_EXTENDEDKEY
        if up:
            flags |= _KEYEVENTF_KEYUP
        structure = _Input(
            type=_INPUT_KEYBOARD,
            union=_InputUnion(
                ki=_KeyBdInput(
                    wVk=0,
                    wScan=scancode,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=ctypes.pointer(_ULONG_PTR(0)),
                )
            ),
        )
        _send(structure)

    def key_down(self, key: str) -> None:  # pragma: no cover
        self._key_event(key, up=False)

    def key_up(self, key: str) -> None:  # pragma: no cover
        self._key_event(key, up=True)

    def _mouse_event(  # pragma: no cover
        self, flags: int, dx: int = 0, dy: int = 0, data: int = 0
    ) -> None:
        structure = _Input(
            type=_INPUT_MOUSE,
            union=_InputUnion(
                mi=_MouseInput(
                    dx=dx,
                    dy=dy,
                    mouseData=data,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=ctypes.pointer(_ULONG_PTR(0)),
                )
            ),
        )
        _send(structure)

    def mouse_move(self, dx: int, dy: int) -> None:  # pragma: no cover
        # Relative, never absolute -- absolute motion does not turn a 3D camera.
        self._mouse_event(_MOUSEEVENTF_MOVE, dx=dx, dy=dy)

    def mouse_down(self, button: str) -> None:  # pragma: no cover
        self._mouse_event(_BUTTON_FLAGS[button][0])

    def mouse_up(self, button: str) -> None:  # pragma: no cover
        self._mouse_event(_BUTTON_FLAGS[button][1])

    def scroll(self, clicks: int) -> None:  # pragma: no cover
        self._mouse_event(_MOUSEEVENTF_WHEEL, data=clicks * 120)


class RecordingInputBackend(InputBackend):
    """Records what would have been sent. Used for dry runs and tests."""

    name = "recording"

    def __init__(self) -> None:
        self.events: list[tuple] = []

    @property
    def available(self) -> bool:
        return True

    def key_down(self, key: str) -> None:
        resolve_scancode(key)
        self.events.append(("key_down", key))

    def key_up(self, key: str) -> None:
        resolve_scancode(key)
        self.events.append(("key_up", key))

    def mouse_move(self, dx: int, dy: int) -> None:
        self.events.append(("mouse_move", dx, dy))

    def mouse_down(self, button: str) -> None:
        self.events.append(("mouse_down", button))

    def mouse_up(self, button: str) -> None:
        self.events.append(("mouse_up", button))

    def scroll(self, clicks: int) -> None:
        self.events.append(("scroll", clicks))


def resolve_scancode(key: str) -> int:
    """Map a key name to its scancode, raising a helpful error if unknown."""
    normalised = str(key).strip().lower()
    if normalised not in SCANCODES:
        raise InputError(
            f"unknown key {key!r}; known keys: {', '.join(sorted(SCANCODES))}"
        )
    return SCANCODES[normalised]


class Keyboard:
    """Timed, bounded key and mouse control over a backend."""

    def __init__(self, backend: InputBackend | None = None) -> None:
        if backend is None:
            backend = WindowsInputBackend() if IS_WINDOWS else RecordingInputBackend()
        self.backend = backend
        self._held: set[str] = set()

    # ---- keys ------------------------------------------------------------

    def tap(self, key: str, duration: float = 0.05) -> None:
        duration = _clamp_duration(duration)
        self.backend.key_down(key)
        try:
            _sleep(duration)
        finally:
            self.backend.key_up(key)

    def hold(self, key: str) -> None:
        """Press and keep held until :meth:`release` or :meth:`release_all`."""
        if key in self._held:
            return
        self.backend.key_down(key)
        self._held.add(key)

    def release(self, key: str) -> None:
        if key not in self._held:
            return
        self.backend.key_up(key)
        self._held.discard(key)

    def release_all(self) -> None:
        """Drop every held key.

        Called on shutdown and after every failed action. Without this, a crash
        mid-hold leaves the character sprinting into terrain indefinitely.
        """
        for key in list(self._held):
            try:
                self.backend.key_up(key)
            except InputError:
                pass
        self._held.clear()

    @property
    def held(self) -> frozenset[str]:
        return frozenset(self._held)

    def chord(self, keys: Iterable[str], duration: float = 0.05) -> None:
        """Press several keys together, e.g. shift+w to sprint forward."""
        keys = list(keys)
        for key in keys:
            self.backend.key_down(key)
        try:
            _sleep(_clamp_duration(duration))
        finally:
            for key in reversed(keys):
                self.backend.key_up(key)

    # ---- mouse -----------------------------------------------------------

    def look(
        self, dx: int, dy: int, *, steps: int = 8, step_delay: float = 0.004
    ) -> None:
        """Turn the camera by a relative delta.

        Split into small increments: a single large jump reads as a teleport to
        the engine's input smoothing and often gets partially discarded.
        """
        dx = _clamp_delta(dx)
        dy = _clamp_delta(dy)
        steps = max(1, int(steps))
        base_x, base_y = dx // steps, dy // steps
        remainder_x, remainder_y = dx - base_x * steps, dy - base_y * steps
        for index in range(steps):
            step_x = base_x + (remainder_x if index == steps - 1 else 0)
            step_y = base_y + (remainder_y if index == steps - 1 else 0)
            if step_x or step_y:
                self.backend.mouse_move(step_x, step_y)
            if step_delay:
                _sleep(step_delay)

    def click(self, button: str = "left", duration: float = 0.05) -> None:
        button = _normalise_button(button)
        self.backend.mouse_down(button)
        try:
            _sleep(_clamp_duration(duration))
        finally:
            self.backend.mouse_up(button)

    def mouse_hold(self, button: str = "left") -> None:
        self.backend.mouse_down(_normalise_button(button))

    def mouse_release(self, button: str = "left") -> None:
        self.backend.mouse_up(_normalise_button(button))

    def scroll(self, clicks: int) -> None:
        self.backend.scroll(int(clicks))


def _normalise_button(button: str) -> str:
    normalised = str(button).strip().lower()
    if normalised not in _BUTTON_FLAGS:
        raise InputError(
            f"unknown mouse button {button!r}; expected one of {MOUSE_BUTTONS}"
        )
    return normalised


def _clamp_duration(duration: float) -> float:
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return 0.05
    return max(0.0, min(MAX_HOLD_SECONDS, value))


def _clamp_delta(delta: int) -> int:
    try:
        value = int(delta)
    except (TypeError, ValueError):
        return 0
    return max(-MAX_MOUSE_DELTA, min(MAX_MOUSE_DELTA, value))


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
