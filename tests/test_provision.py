"""Tests for PalWorldSettings.ini generation and parsing."""

from __future__ import annotations

import pytest

from rappter_plays_palworld.provision import (
    AGENT_SERVER_DEFAULTS,
    SECTION,
    ProvisionError,
    build_option_settings,
    build_settings_ini,
    format_value,
    parse_option_settings,
    validate,
    write_settings_ini,
)

GOOD = {"AdminPassword": "a-sufficiently-long-secret"}


class TestFormatValue:
    def test_booleans_use_palworld_casing(self):
        assert format_value(True) == "True"
        assert format_value(False) == "False"

    def test_floats_get_six_decimals(self):
        assert format_value(1.0) == "1.000000"

    def test_integers_are_bare(self):
        assert format_value(8212) == "8212"

    def test_free_text_is_quoted_even_when_a_single_word(self):
        # A bare unquoted string breaks Palworld's parser for the whole line.
        assert format_value("Test", "ServerName") == '"Test"'
        assert format_value("My Server", "ServerName") == '"My Server"'

    def test_enum_keys_stay_bare(self):
        assert format_value("Json", "LogFormatType") == "Json"
        assert format_value("Item", "DeathPenalty") == "Item"

    def test_tuple_values_stay_bare(self):
        assert format_value("(Steam,Xbox)", "CrossplayPlatforms") == "(Steam,Xbox)"

    def test_embedded_quotes_are_escaped(self):
        assert format_value('a"b', "ServerName") == '"a\\"b"'


class TestValidate:
    def test_defaults_plus_password_pass(self):
        validate({**AGENT_SERVER_DEFAULTS, **GOOD})

    def test_rest_api_must_be_enabled(self):
        settings = {**AGENT_SERVER_DEFAULTS, **GOOD, "RESTAPIEnabled": False}
        with pytest.raises(ProvisionError, match="RESTAPIEnabled must be True"):
            validate(settings)

    def test_password_is_required(self):
        settings = {**AGENT_SERVER_DEFAULTS, "AdminPassword": ""}
        with pytest.raises(ProvisionError, match="AdminPassword must be set"):
            validate(settings)

    def test_short_password_is_rejected(self):
        settings = {**AGENT_SERVER_DEFAULTS, "AdminPassword": "short"}
        with pytest.raises(ProvisionError, match="at least 12 characters"):
            validate(settings)

    def test_missing_keys_are_reported(self):
        with pytest.raises(ProvisionError, match="missing agent-critical settings"):
            validate({"AdminPassword": "a-sufficiently-long-secret"})

    def test_port_range_is_checked(self):
        settings = {**AGENT_SERVER_DEFAULTS, **GOOD, "RESTAPIPort": 70000}
        with pytest.raises(ProvisionError, match="out of range"):
            validate(settings)

    def test_player_cap_is_enforced(self):
        settings = {**AGENT_SERVER_DEFAULTS, **GOOD, "ServerPlayerMaxNum": 64}
        with pytest.raises(ProvisionError, match="exceeds the documented"):
            validate(settings)


class TestBuild:
    def test_ini_has_the_required_section_header(self):
        body = build_settings_ini(GOOD)
        assert body.startswith(SECTION)
        assert "OptionSettings=(" in body

    def test_option_settings_is_a_single_line(self):
        body = build_settings_ini(GOOD)
        option_lines = [
            line for line in body.splitlines() if line.startswith("OptionSettings")
        ]
        assert len(option_lines) == 1

    def test_overrides_win(self):
        line = build_option_settings({**GOOD, "ServerName": "Override"})
        assert 'ServerName="Override"' in line

    def test_build_validates(self):
        with pytest.raises(ProvisionError):
            build_settings_ini({"AdminPassword": ""})


class TestRoundTrip:
    def test_generated_ini_parses_back(self):
        body = build_settings_ini({**GOOD, "ServerName": "Round Trip"})
        parsed = parse_option_settings(body)
        assert parsed["ServerName"] == "Round Trip"
        assert parsed["RESTAPIEnabled"] == "True"
        assert parsed["RESTAPIPort"] == "8212"
        assert parsed["AdminPassword"] == "a-sufficiently-long-secret"

    def test_parse_handles_nested_parentheses(self):
        text = (
            "[/Script/Pal.PalGameWorldSettings]\n"
            'OptionSettings=(ServerName="X",CrossplayPlatforms=(Steam,Xbox),RCONPort=25575)'
        )
        parsed = parse_option_settings(text)
        assert parsed["CrossplayPlatforms"] == "(Steam,Xbox)"
        assert parsed["RCONPort"] == "25575"
        assert parsed["ServerName"] == "X"

    def test_parse_returns_empty_without_option_settings(self):
        assert parse_option_settings("[/Script/Pal.PalGameWorldSettings]") == {}


class TestWrite:
    def test_writes_and_backs_up(self, tmp_path):
        target = tmp_path / "PalWorldSettings.ini"
        write_settings_ini(target, GOOD)
        assert target.is_file()

        write_settings_ini(target, {**GOOD, "ServerName": "Second"})
        backup = tmp_path / "PalWorldSettings.ini.bak"
        assert backup.is_file()
        assert "Second" in target.read_text()
        assert "Second" not in backup.read_text()

    def test_refuses_a_missing_config_directory(self, tmp_path):
        target = tmp_path / "nope" / "PalWorldSettings.ini"
        with pytest.raises(ProvisionError, match="config directory does not exist"):
            write_settings_ini(target, GOOD)
