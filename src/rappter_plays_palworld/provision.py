"""Generate PalWorldSettings.ini for an agent-ready dedicated server.

Palworld stores every tunable in a single ``OptionSettings=(...)`` line under
``[/Script/Pal.PalGameWorldSettings]``. Hand-editing that line is how people
corrupt their config, so this module builds it from a dict and validates the
handful of keys that actually gate agent access.

Config file locations (the directories only exist after the first server boot):
    Windows  steamapps\\common\\PalServer\\Pal\\Saved\\Config\\WindowsServer\\PalWorldSettings.ini
    Linux    steamapps/common/PalServer/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini

Editing DefaultPalWorldSettings.ini has no effect -- it is a sample only.

Reference: https://docs.palworldgame.com/settings-and-operation/configuration
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

SECTION = "[/Script/Pal.PalGameWorldSettings]"

# Keys the agent cannot function without. RESTAPIEnabled gates the entire
# perception layer; AdminPassword is the Basic-auth secret.
REQUIRED_FOR_AGENT = ("RESTAPIEnabled", "RESTAPIPort", "AdminPassword")

# Defaults tuned for an agent-populated server rather than a human one.
# Everything else is left at Pocketpair's shipped default deliberately.
AGENT_SERVER_DEFAULTS: dict[str, Any] = {
    "ServerName": "RAPPter Plays Palworld",
    "ServerDescription": "Autonomous agents on a self-hosted world.",
    "ServerPlayerMaxNum": 32,
    # Perception + admin surface. Pocketpair: "These APIs are not designed to
    # be exposed directly to the Internet ... recommended ... within the LAN."
    "RESTAPIEnabled": True,
    "RESTAPIPort": 8212,
    # RCON is deprecated and "scheduled to stop functioning in an upcoming
    # update" -- do not build on it.
    "RCONEnabled": False,
    "RCONPort": 25575,
    # Agents need to see each other and be seen.
    "bShowPlayerList": True,
    "bIsShowJoinLeftMessage": True,
    # An agent that logs out should not leave a vulnerable sleeping body.
    "bExistPlayerAfterLogout": False,
    # Long-lived world hygiene. Backups cost disk I/O but the docs warn that
    # slow storage corrupts saves, and a 64GB SSD box can afford it.
    "bIsUseBackupSaveData": True,
    # Machine-readable logs so the agent can tail them.
    "LogFormatType": "Json",
    # Agents chatter more than humans; the default 30/min throttles them.
    "ChatPostLimitPerMinute": 60,
    # Keep the world-wide base ceiling clear of the 128 hard cap when many
    # solo agents each hold their own guild.
    "BaseCampMaxNumInGuild": 4,
    "BaseCampMaxNum": 128,
}


class ProvisionError(RuntimeError):
    """The requested server configuration is not usable by the agent."""


# Keys whose string values are bare enums/identifiers, written without quotes.
# Everything else that is a string gets quoted, because Palworld treats an
# unquoted free-text value as a parse error and silently drops the whole line.
ENUM_KEYS = frozenset(
    {
        "Difficulty",
        "DeathPenalty",
        "RandomizerType",
        "LogFormatType",
        "AllowConnectPlatform",
        "CrossplayPlatforms",
    }
)


def format_value(value: Any, key: str = "") -> str:
    """Render one OptionSettings value using Palworld's own conventions."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Palworld writes floats with six decimal places.
        return f"{value:.6f}"

    text = str(value)
    # Tuple-valued settings such as CrossplayPlatforms=(Steam,Xbox) stay bare.
    if text.startswith("(") and text.endswith(")"):
        return text
    if key in ENUM_KEYS:
        return text
    return '"' + text.replace('"', '\\"') + '"'


def build_option_settings(overrides: Mapping[str, Any] | None = None) -> str:
    """Build the single ``OptionSettings=(...)`` line."""
    merged: dict[str, Any] = dict(AGENT_SERVER_DEFAULTS)
    merged.update(overrides or {})
    rendered = [f"{key}={format_value(value, key)}" for key, value in merged.items()]
    return "OptionSettings=(" + ",".join(rendered) + ")"


def build_settings_ini(overrides: Mapping[str, Any] | None = None) -> str:
    """Build a complete PalWorldSettings.ini body."""
    merged: dict[str, Any] = dict(AGENT_SERVER_DEFAULTS)
    merged.update(overrides or {})
    validate(merged)
    return f"{SECTION}\n{build_option_settings(merged)}\n"


def validate(settings: Mapping[str, Any]) -> None:
    """Reject configurations the agent cannot actually drive."""
    missing = [key for key in REQUIRED_FOR_AGENT if key not in settings]
    if missing:
        raise ProvisionError(
            "missing agent-critical settings: " + ", ".join(sorted(missing))
        )
    if not settings.get("RESTAPIEnabled"):
        raise ProvisionError(
            "RESTAPIEnabled must be True; it is the agent's only perception channel"
        )
    password = str(settings.get("AdminPassword") or "")
    if not password:
        raise ProvisionError(
            "AdminPassword must be set; the REST API accepts only Basic auth"
        )
    if len(password) < 12:
        raise ProvisionError(
            "AdminPassword must be at least 12 characters -- it is the sole "
            "credential protecting kick/ban/shutdown"
        )
    port = int(settings.get("RESTAPIPort") or 0)
    if not 1 <= port <= 65535:
        raise ProvisionError(f"RESTAPIPort is out of range: {port}")
    if int(settings.get("ServerPlayerMaxNum") or 0) > 32:
        raise ProvisionError(
            "ServerPlayerMaxNum above 32 exceeds the documented dedicated-server cap"
        )


def write_settings_ini(
    destination: Path,
    overrides: Mapping[str, Any] | None = None,
) -> Path:
    """Write PalWorldSettings.ini, refusing to clobber blindly."""
    destination = Path(destination).expanduser()
    body = build_settings_ini(overrides)
    if not destination.parent.is_dir():
        raise ProvisionError(
            f"config directory does not exist yet: {destination.parent}\n"
            "Start the server once so it creates Pal/Saved/Config, then rerun."
        )
    if destination.exists():
        backup = destination.with_suffix(".ini.bak")
        backup.write_text(destination.read_text(encoding="utf-8"), encoding="utf-8")
    destination.write_text(body, encoding="utf-8")
    return destination


def parse_option_settings(text: str) -> dict[str, str]:
    """Parse an existing OptionSettings line back into a dict.

    Values are returned as raw strings (quotes stripped) because the ini format
    carries no type information.
    """
    match = re.search(r"OptionSettings\s*=\s*\((.*)\)", text, re.DOTALL)
    if not match:
        return {}
    body = match.group(1)

    settings: dict[str, str] = {}
    depth = 0
    current = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            _absorb(current, settings)
            current = []
            continue
        current.append(char)
    _absorb(current, settings)
    return settings


def _absorb(chars: list[str], settings: dict[str, str]) -> None:
    entry = "".join(chars).strip()
    if not entry or "=" not in entry:
        return
    key, _, value = entry.partition("=")
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    settings[key.strip()] = value
