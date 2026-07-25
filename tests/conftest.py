"""Shared fixtures built from the payload examples in the official API docs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def info_payload() -> dict:
    """Verbatim from https://docs.palworldgame.com/api/rest-api/info"""
    return {
        "version": "v0.1.5.0",
        "servername": "Palworld example Server",
        "description": "This is a Palworld server.",
        "worldguid": "A7E97BAA767DB9029EF013BB71E993A0",
    }


@pytest.fixture
def players_payload() -> dict:
    """Verbatim from https://docs.palworldgame.com/api/rest-api/players"""
    return {
        "players": [
            {
                "name": "PalUser",
                "accountName": "paluser",
                "playerId": "AFAFD830000000000000000000000000",
                "userId": "steam_00000000000000000",
                "ip": "127.0.0.1",
                "ping": 3.14,
                "location_x": 123.45,
                "location_y": 67.89,
                "level": 1,
                "building_count": 119,
            }
        ]
    }


@pytest.fixture
def metrics_payload() -> dict:
    """Verbatim from https://docs.palworldgame.com/api/rest-api/metrics"""
    return {
        "serverfps": 57,
        "currentplayernum": 10,
        "serverframetime": 16.7671,
        "maxplayernum": 32,
        "uptime": 3600,
        "basecampnum": 32,
        "days": 1,
    }


def _character(
    instance_id: str,
    unit_type: str = "Player",
    *,
    nickname: str = "",
    hp: int = 100,
    max_hp: int = 100,
    level: int = 10,
    guild_id: str = "G1",
    guild_name: str = "Guild One",
    userid: str = "",
    location=(0.0, 0.0, 0.0),
    action: str = "",
) -> dict:
    return {
        "Type": "Character",
        "InstanceID": instance_id,
        "UnitType": unit_type,
        "NickName": nickname or instance_id,
        "userid": userid,
        "level": level,
        "HP": hp,
        "MaxHP": max_hp,
        "GuildID": guild_id,
        "GuildName": guild_name,
        "Class": "PlayerCharacter" if unit_type == "Player" else "Lamball",
        "Action": action,
        "AI_Action": "",
        "LocationX": location[0],
        "LocationY": location[1],
        "LocationZ": location[2],
        "RotationX": 0.0,
        "RotationY": 0.0,
        "RotationZ": 0.0,
        "Stage": "",
        "IsActive": "true",
    }


@pytest.fixture
def character_factory():
    return _character


@pytest.fixture
def game_data_payload() -> dict:
    """Shaped after https://docs.palworldgame.com/api/rest-api/game-data"""
    return {
        "Time": "2026-06-17 13:00:40",
        "FPS": 91.71,
        "AverageFPS": 33.78,
        "ActorData": [
            _character(
                "P1",
                "Player",
                nickname="Kody",
                userid="steam_1",
                location=(10000.0, 20000.0, 300.0),
                action="Running",
            ),
            _character(
                "PAL1",
                "OtomoPal",
                nickname="Lamball",
                level=5,
                location=(10100.0, 20050.0, 300.0),
            ),
            _character("WILD1", "WildPal", nickname="Chikipi", level=2),
            {
                "Type": "PalBox",
                "GuildID": "G1",
                "GuildName": "Guild One",
                "Class": "PalBox",
                "LocationX": 9000.0,
                "LocationY": 19000.0,
                "LocationZ": 300.0,
            },
        ],
    }
