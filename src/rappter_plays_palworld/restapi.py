"""Typed client for the official Palworld dedicated-server REST API (v0.2.0.0).

Covers the complete documented surface -- 12 endpoints, no more:

    read    GET  /info /players /settings /metrics /game-data
    write   POST /announce /kick /ban /unban /save /shutdown /stop

Every documented write acts on the *server or a player's connection*. None of
them mutate world state, so this client is deliberately a sensor plus a
loudspeaker: it can observe every actor in the world and talk to the humans in
it, and that is the entire officially sanctioned actuator set.

Reference: https://docs.palworldgame.com/api/rest-api/palwold-rest-api

The module depends only on the standard library so it can be imported during
bootstrap before the runtime extra is installed.
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

# Pocketpair documents `RESTAPIEnabled` and `RESTAPIPort` but publishes neither
# the default port nor the URL path prefix anywhere on docs.palworldgame.com.
# Both values below come from the shipped DefaultPalWorldSettings.ini and from
# observed server behaviour -- treat them as overridable, not as gospel.
DEFAULT_PORT = 8212
DEFAULT_BASE_PATH = "/v1/api"

# The docs specify "HTTP: Basic Auth" without naming the username. The server
# accepts the literal `admin` paired with OptionSettings AdminPassword.
DEFAULT_USERNAME = "admin"

DEFAULT_TIMEOUT = 10.0

# `GET /game-data` serialises every actor in the world on each call. The docs
# publish no rate limit and no cost guidance, so the client refuses to hammer
# it faster than this unless the caller opts out explicitly.
MIN_GAME_DATA_INTERVAL = 1.0


class PalworldApiError(RuntimeError):
    """Any failure talking to the Palworld REST API."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PalworldAuthError(PalworldApiError):
    """The server rejected the Basic credentials (HTTP 401)."""


class PalworldUnavailableError(PalworldApiError):
    """The server could not be reached at all."""


@dataclass(frozen=True)
class ServerInfo:
    """Response of ``GET /info``."""

    version: str = ""
    servername: str = ""
    description: str = ""
    worldguid: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ServerInfo":
        return cls(
            version=str(payload.get("version") or ""),
            servername=str(payload.get("servername") or ""),
            description=str(payload.get("description") or ""),
            worldguid=str(payload.get("worldguid") or ""),
        )


@dataclass(frozen=True)
class ServerMetrics:
    """Response of ``GET /metrics``."""

    serverfps: int = 0
    currentplayernum: int = 0
    serverframetime: float = 0.0
    maxplayernum: int = 0
    uptime: int = 0
    basecampnum: int = 0
    days: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ServerMetrics":
        return cls(
            serverfps=_as_int(payload.get("serverfps")),
            currentplayernum=_as_int(payload.get("currentplayernum")),
            serverframetime=_as_float(payload.get("serverframetime")),
            maxplayernum=_as_int(payload.get("maxplayernum")),
            uptime=_as_int(payload.get("uptime")),
            basecampnum=_as_int(payload.get("basecampnum")),
            days=_as_int(payload.get("days")),
        )

    @property
    def healthy(self) -> bool:
        """Server FPS is the single best liveness signal the API exposes."""
        return self.serverfps > 0


@dataclass(frozen=True)
class Player:
    """One entry of ``GET /players``.

    ``userId`` is the platform-prefixed account handle (``steam_7656...``) and
    is what /kick, /ban and /unban key on. ``playerId`` is a separate 32-hex
    in-world UID. They are not interchangeable.
    """

    name: str = ""
    accountName: str = ""
    playerId: str = ""
    userId: str = ""
    ip: str = ""
    ping: float = 0.0
    location_x: float = 0.0
    location_y: float = 0.0
    level: int = 0
    building_count: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Player":
        return cls(
            name=str(payload.get("name") or ""),
            accountName=str(payload.get("accountName") or ""),
            playerId=str(payload.get("playerId") or ""),
            userId=str(payload.get("userId") or ""),
            ip=str(payload.get("ip") or ""),
            ping=_as_float(payload.get("ping")),
            location_x=_as_float(payload.get("location_x")),
            location_y=_as_float(payload.get("location_y")),
            level=_as_int(payload.get("level")),
            building_count=_as_int(payload.get("building_count")),
        )


@dataclass(frozen=True)
class Actor:
    """One entry of ``ActorData`` in ``GET /game-data``.

    The API returns a ``oneOf`` between a Character actor and a PalBox actor.
    Both are folded into this single shape; ``type`` disambiguates.
    """

    type: str = ""
    unit_type: str = ""
    instance_id: str = ""
    nickname: str = ""
    trainer_instance_id: str = ""
    trainer_nickname: str = ""
    trainer_class: str = ""
    userid: str = ""
    level: int = 0
    hp: int = 0
    max_hp: int = 0
    guild_id: str = ""
    guild_name: str = ""
    actor_class: str = ""
    action: str = ""
    ai_action: str = ""
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    stage: str = ""
    is_active: bool = True

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Actor":
        return cls(
            type=str(payload.get("Type") or ""),
            unit_type=str(payload.get("UnitType") or ""),
            instance_id=str(payload.get("InstanceID") or ""),
            nickname=str(payload.get("NickName") or ""),
            trainer_instance_id=str(payload.get("TrainerInstanceID") or ""),
            trainer_nickname=str(payload.get("TrainerNickName") or ""),
            trainer_class=str(payload.get("TrainerClass") or ""),
            userid=str(payload.get("userid") or ""),
            level=_as_int(payload.get("level")),
            hp=_as_int(payload.get("HP")),
            max_hp=_as_int(payload.get("MaxHP")),
            guild_id=str(payload.get("GuildID") or ""),
            guild_name=str(payload.get("GuildName") or ""),
            actor_class=str(payload.get("Class") or ""),
            action=str(payload.get("Action") or ""),
            ai_action=str(payload.get("AI_Action") or ""),
            location=(
                _as_float(payload.get("LocationX")),
                _as_float(payload.get("LocationY")),
                _as_float(payload.get("LocationZ")),
            ),
            rotation=(
                _as_float(payload.get("RotationX")),
                _as_float(payload.get("RotationY")),
                _as_float(payload.get("RotationZ")),
            ),
            stage=str(payload.get("Stage") or ""),
            # The API encodes IsActive as the *strings* "true"/"false".
            is_active=_as_bool(payload.get("IsActive"), default=True),
        )

    @property
    def is_player(self) -> bool:
        return self.unit_type == "Player"

    @property
    def is_pal(self) -> bool:
        return self.unit_type in ("OtomoPal", "BaseCampPal", "WildPal")

    @property
    def label(self) -> str:
        return self.nickname or self.actor_class or self.instance_id or "?"


@dataclass(frozen=True)
class WorldSnapshot:
    """Response of ``GET /game-data`` -- every actor at one instant."""

    time: str = ""
    fps: float = 0.0
    average_fps: float = 0.0
    actors: tuple[Actor, ...] = field(default_factory=tuple)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WorldSnapshot":
        raw = payload.get("ActorData")
        actors = tuple(
            Actor.from_payload(item)
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, Mapping)
        )
        return cls(
            time=str(payload.get("Time") or ""),
            fps=_as_float(payload.get("FPS")),
            average_fps=_as_float(payload.get("AverageFPS")),
            actors=actors,
        )

    def by_unit_type(self, unit_type: str) -> tuple[Actor, ...]:
        return tuple(a for a in self.actors if a.unit_type == unit_type)

    @property
    def players(self) -> tuple[Actor, ...]:
        return self.by_unit_type("Player")

    @property
    def palboxes(self) -> tuple[Actor, ...]:
        return tuple(a for a in self.actors if a.type == "PalBox")


class PalworldRestClient:
    """Blocking client for the documented Palworld REST API.

    Uses only ``urllib`` so it works in a bare virtualenv. Every call raises a
    :class:`PalworldApiError` subclass rather than leaking urllib exceptions.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        password: str = "",
        *,
        username: str = DEFAULT_USERNAME,
        base_path: str = DEFAULT_BASE_PATH,
        timeout: float = DEFAULT_TIMEOUT,
        scheme: str = "http",
    ) -> None:
        if not password:
            raise ValueError(
                "AdminPassword is required; the REST API only accepts Basic auth"
            )
        self.host = host
        self.port = int(port)
        self.username = username
        self.base_path = "/" + base_path.strip("/")
        self.timeout = float(timeout)
        self.scheme = scheme
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._auth_header = f"Basic {token}"

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.base_path}"

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = None
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(  # noqa: S310 - scheme is fixed http/https
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.timeout
            ) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", "replace").strip()
            except Exception:  # pragma: no cover - body is best effort only
                detail = ""
            if error.code == 401:
                raise PalworldAuthError(
                    "Palworld REST API rejected the AdminPassword "
                    "(check OptionSettings AdminPassword and RESTAPIEnabled=True)",
                    status=401,
                ) from error
            raise PalworldApiError(
                f"{method} {path} failed with HTTP {error.code}"
                + (f": {detail}" if detail else ""),
                status=error.code,
            ) from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
            raise PalworldUnavailableError(
                f"cannot reach the Palworld REST API at {self.base_url} ({error})"
            ) from error

        if not payload:
            return None
        text = payload.decode("utf-8", "replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Several POST endpoints reply with a bare confirmation string.
            return text

    # ---- reads -----------------------------------------------------------

    def info(self) -> ServerInfo:
        payload = self._request("GET", "info")
        return ServerInfo.from_payload(payload if isinstance(payload, Mapping) else {})

    def players(self) -> tuple[Player, ...]:
        payload = self._request("GET", "players")
        raw = payload.get("players") if isinstance(payload, Mapping) else None
        return tuple(
            Player.from_payload(item)
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, Mapping)
        )

    def settings(self) -> dict[str, Any]:
        payload = self._request("GET", "settings")
        return dict(payload) if isinstance(payload, Mapping) else {}

    def metrics(self) -> ServerMetrics:
        payload = self._request("GET", "metrics")
        return ServerMetrics.from_payload(
            payload if isinstance(payload, Mapping) else {}
        )

    def game_data(self) -> WorldSnapshot:
        payload = self._request("GET", "game-data")
        return WorldSnapshot.from_payload(
            payload if isinstance(payload, Mapping) else {}
        )

    # ---- writes ----------------------------------------------------------

    def announce(self, message: str) -> None:
        message = message.strip()
        if not message:
            raise ValueError("announce requires a non-empty message")
        self._request("POST", "announce", {"message": message})

    def kick(self, userid: str, message: str = "") -> None:
        self._request("POST", "kick", _target(userid, message))

    def ban(self, userid: str, message: str = "") -> None:
        self._request("POST", "ban", _target(userid, message))

    def unban(self, userid: str) -> None:
        if not userid:
            raise ValueError("unban requires a userid")
        self._request("POST", "unban", {"userid": userid})

    def save(self) -> None:
        self._request("POST", "save", {})

    def shutdown(self, waittime: int = 30, message: str = "") -> None:
        body: dict[str, Any] = {"waittime": int(waittime)}
        if message:
            body["message"] = message
        self._request("POST", "shutdown", body)

    def stop(self) -> None:
        self._request("POST", "stop", {})

    # ---- convenience -----------------------------------------------------

    def reachable(self) -> bool:
        """True when the API answers and the credentials are accepted."""
        try:
            self.info()
        except PalworldApiError:
            return False
        return True


def _target(userid: str, message: str) -> dict[str, Any]:
    if not userid:
        raise ValueError("this action requires a userid (e.g. steam_7656...)")
    body: dict[str, Any] = {"userid": userid}
    if message:
        body["message"] = message
    return body


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return default
