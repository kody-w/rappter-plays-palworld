"""Screen capture of the live game window.

The agent sees what a player sees. That means grabbing the game's window
contents, not a headless render, and doing it fast enough that the frame the
model reasons about still resembles the world by the time it acts.

Capture targets the game window specifically rather than the whole desktop, so
notifications, the agent's own console, and a second monitor never leak into
the model's view.

``mss`` is used for pixels because it is fast and cross-platform. Locating the
window is Windows-specific and done through ctypes, with a full-screen fallback
everywhere else so the module stays importable and testable off-Windows.
"""

from __future__ import annotations

import ctypes
import io
import platform
import time
from dataclasses import dataclass
from typing import Optional

IS_WINDOWS = platform.system() == "Windows"

# Frames are downscaled before they reach the model. Native 1440p costs a lot of
# tokens and buys very little: the model needs to read the HUD and recognise
# terrain, not count blades of grass.
DEFAULT_MAX_WIDTH = 1280
DEFAULT_JPEG_QUALITY = 80


class CaptureError(RuntimeError):
    """The game window could not be captured."""


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    width: int
    height: int

    @property
    def area(self) -> int:
        return self.width * self.height

    def as_mss_region(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class Frame:
    """One captured frame, already encoded for transport to the model."""

    data: bytes
    width: int
    height: int
    captured_at: float
    media_type: str = "image/jpeg"

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.captured_at


def find_window(title: str) -> Optional[WindowRect]:
    """Locate a window by exact or partial title match.

    Returns None when the window is absent, so callers can distinguish "game not
    running" from "capture failed".
    """
    if not IS_WINDOWS:
        return None
    return _find_window_windows(title)


def _find_window_windows(title: str) -> Optional[WindowRect]:  # pragma: no cover
    user32 = ctypes.windll.user32

    # Exact match first -- cheap, and correct when the title is known.
    handle = user32.FindWindowW(None, title)

    if not handle:
        # Fall back to a substring scan, because game windows often append
        # build or version text to the title.
        matches: list[int] = []
        needle = title.lower()

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
        )

        def _callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if needle in buffer.value.lower():
                matches.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(_callback), None)
        if not matches:
            return None
        handle = matches[0]

    class _Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = _Rect()
    # GetClientRect + ClientToScreen excludes the title bar and borders, so the
    # model never sees window chrome.
    if not user32.GetClientRect(handle, ctypes.byref(rect)):
        return None

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    origin = _Point(0, 0)
    user32.ClientToScreen(handle, ctypes.byref(origin))

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return WindowRect(left=origin.x, top=origin.y, width=width, height=height)


class ScreenCapture:
    """Captures the game window as JPEG frames.

    ``mss`` and ``Pillow`` are imported lazily so that importing this module --
    and therefore the whole agent -- never depends on them being installed.
    """

    def __init__(
        self,
        window_title: str,
        *,
        max_width: int = DEFAULT_MAX_WIDTH,
        quality: int = DEFAULT_JPEG_QUALITY,
        allow_fullscreen_fallback: bool = True,
    ) -> None:
        self.window_title = window_title
        self.max_width = int(max_width)
        self.quality = int(quality)
        self.allow_fullscreen_fallback = allow_fullscreen_fallback
        self._sct = None
        self._last_rect: Optional[WindowRect] = None

    def _ensure_sct(self):
        if self._sct is not None:
            return self._sct
        try:
            import mss  # type: ignore
        except ImportError as error:
            raise CaptureError(
                "mss is not installed; install the runtime extra: "
                "pip install -e '.[runtime]'"
            ) from error
        self._sct = mss.mss()
        return self._sct

    def locate(self) -> WindowRect:
        """Find the game window, or fall back to the primary monitor."""
        rect = find_window(self.window_title)
        if rect is not None:
            self._last_rect = rect
            return rect

        if not self.allow_fullscreen_fallback:
            raise CaptureError(
                f"could not find a window titled {self.window_title!r}. "
                "Is the game running, and is the agent on the same desktop session?"
            )

        sct = self._ensure_sct()
        monitor = sct.monitors[1]
        return WindowRect(
            left=monitor["left"],
            top=monitor["top"],
            width=monitor["width"],
            height=monitor["height"],
        )

    def grab(self) -> Frame:
        """Capture one frame and encode it as JPEG."""
        sct = self._ensure_sct()
        rect = self.locate()
        captured_at = time.monotonic()

        raw = sct.grab(rect.as_mss_region())

        try:
            from PIL import Image  # type: ignore
        except ImportError as error:
            raise CaptureError(
                "Pillow is not installed; install the runtime extra: "
                "pip install -e '.[runtime]'"
            ) from error

        image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        if self.max_width and image.width > self.max_width:
            height = round(image.height * (self.max_width / image.width))
            image = image.resize((self.max_width, height), Image.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self.quality, optimize=True)

        return Frame(
            data=buffer.getvalue(),
            width=image.width,
            height=image.height,
            captured_at=captured_at,
        )

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._sct = None

    def __enter__(self) -> "ScreenCapture":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
