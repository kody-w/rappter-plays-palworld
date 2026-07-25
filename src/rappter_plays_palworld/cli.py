"""Command-line front end for the Palworld warden agent."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .provision import ProvisionError, build_settings_ini, parse_option_settings
from .restapi import DEFAULT_PORT, PalworldApiError, PalworldRestClient

AGENT_MODULE = "openrappter.agents.palworld_agent"


def load_agent() -> Any:
    """Import the registered agent, falling back to the in-repo copy.

    Running from a checkout should work before ``install_agent`` has been run,
    which is the common case during development.
    """
    try:
        module = importlib.import_module(AGENT_MODULE)
    except ImportError:
        repo_agent = Path(__file__).resolve().parents[2] / "palworld_agent.py"
        if not repo_agent.is_file():
            raise RuntimeError(
                "Cannot find palworld_agent.py. Run ./bootstrap.sh --setup-only."
            ) from None
        sys.path.insert(0, str(repo_agent.parent))
        module = importlib.import_module("palworld_agent")
    return module


def _client_from_args(args: argparse.Namespace) -> PalworldRestClient:
    password = args.password or os.environ.get("PALWORLD_ADMIN_PASSWORD")
    if not password:
        raise SystemExit(
            "error: set PALWORLD_ADMIN_PASSWORD or pass --password.\n"
            "It must match OptionSettings AdminPassword in PalWorldSettings.ini."
        )
    return PalworldRestClient(host=args.host, port=args.rest_port, password=password)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check every precondition the agent depends on, in order."""
    print(f"Palworld REST API check -> {args.host}:{args.rest_port}")
    try:
        client = _client_from_args(args)
    except SystemExit as error:
        print(str(error))
        return 1

    checks: list[tuple[str, bool, str]] = []
    try:
        info = client.info()
        checks.append(
            ("reachable", True, f"{info.servername or '(unnamed)'} v{info.version}")
        )
        checks.append(("world guid", bool(info.worldguid), info.worldguid or "missing"))
    except PalworldApiError as error:
        checks.append(("reachable", False, str(error)))
        _print_checks(checks)
        print()
        print("Fix: confirm the server is running and that PalWorldSettings.ini has")
        print("     RESTAPIEnabled=True and a matching AdminPassword, then restart it.")
        return 1

    try:
        metrics = client.metrics()
        checks.append(
            (
                "metrics",
                metrics.healthy,
                f"{metrics.serverfps} fps, "
                f"{metrics.currentplayernum}/{metrics.maxplayernum} players",
            )
        )
    except PalworldApiError as error:
        checks.append(("metrics", False, str(error)))

    try:
        snapshot = client.game_data()
        checks.append(
            (
                "game-data",
                True,
                f"{len(snapshot.actors)} actors, {len(snapshot.players)} players",
            )
        )
    except PalworldApiError as error:
        checks.append(
            (
                "game-data",
                False,
                f"{error} (this endpoint requires a v1.0+ server)",
            )
        )

    _print_checks(checks)
    return 0 if all(ok for _, ok, _ in checks) else 1


def _print_checks(checks: Sequence[tuple[str, bool, str]]) -> None:
    for label, ok, detail in checks:
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {label:<12} {detail}")


def cmd_config(args: argparse.Namespace) -> int:
    """Render an agent-ready PalWorldSettings.ini to stdout or a file."""
    overrides: dict[str, Any] = {}
    if args.set:
        for pair in args.set:
            key, _, value = pair.partition("=")
            if not key or not value:
                print(f"error: --set expects KEY=VALUE, got {pair!r}")
                return 1
            overrides[key.strip()] = _coerce(value.strip())

    password = args.password or os.environ.get("PALWORLD_ADMIN_PASSWORD")
    if password:
        overrides.setdefault("AdminPassword", password)
    if args.server_name:
        overrides["ServerName"] = args.server_name

    try:
        body = build_settings_ini(overrides)
    except ProvisionError as error:
        print(f"error: {error}")
        return 1

    if args.output:
        destination = Path(args.output).expanduser()
        destination.write_text(body, encoding="utf-8")
        print(f"Wrote {destination}")
    else:
        sys.stdout.write(body)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Parse an existing PalWorldSettings.ini and report the agent-critical keys."""
    path = Path(args.path).expanduser()
    if not path.is_file():
        print(f"error: no such file: {path}")
        return 1
    settings = parse_option_settings(path.read_text(encoding="utf-8"))
    if not settings:
        print("error: no OptionSettings=(...) line found")
        return 1

    if args.json:
        print(json.dumps(settings, indent=2, sort_keys=True))
        return 0

    print(f"{len(settings)} settings in {path}")
    interesting = (
        "ServerName",
        "ServerPlayerMaxNum",
        "RESTAPIEnabled",
        "RESTAPIPort",
        "RCONEnabled",
        "AdminPassword",
        "PublicPort",
        "PublicIP",
        "LogFormatType",
    )
    for key in interesting:
        value = settings.get(key)
        if key == "AdminPassword" and value:
            value = f"<set, {len(value)} chars>"
        print(f"  {key:<22} {value if value is not None else '(unset)'}")

    if settings.get("RESTAPIEnabled", "").lower() != "true":
        print()
        print("warning: RESTAPIEnabled is not True -- the agent cannot perceive")
        print("         anything until it is enabled and the server restarted.")
    return 0


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def cmd_agent(args: argparse.Namespace, extra: Sequence[str]) -> int:
    module = load_agent()
    return int(module.main(list(extra)) or 0)


def build_parser() -> argparse.ArgumentParser:
    # Connection flags are accepted both before and after the subcommand name,
    # because `config --password X` is at least as natural as
    # `--password X config`.
    #
    # The subcommand copies use SUPPRESS defaults deliberately: a plain default
    # would overwrite a value already parsed at the root level, so
    # `--password X doctor` would silently lose the password.
    def connection_flags(*, suppress: bool) -> argparse.ArgumentParser:
        group = argparse.ArgumentParser(add_help=False)
        host_default = (
            argparse.SUPPRESS
            if suppress
            else os.environ.get("PALWORLD_HOST", "127.0.0.1")
        )
        port_default = (
            argparse.SUPPRESS
            if suppress
            else int(os.environ.get("PALWORLD_REST_PORT", str(DEFAULT_PORT)))
        )
        password_default = argparse.SUPPRESS if suppress else None
        group.add_argument("--host", default=host_default)
        group.add_argument("--rest-port", type=int, default=port_default)
        group.add_argument("--password", default=password_default)
        return group

    root_common = connection_flags(suppress=False)
    common = connection_flags(suppress=True)

    parser = argparse.ArgumentParser(
        prog="rappter-plays-palworld",
        parents=[root_common],
        description="Run and operate the Palworld warden agent",
        epilog=(
            "Agent actions (start, status, world, players, metrics, announce, "
            "save, kick, ban, unban, shutdown, stop) are passed through to the "
            "agent itself; run `rappter-plays-palworld start --help` for those."
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser(
        "doctor", parents=[common], help="Verify the server is agent-ready"
    )
    doctor.set_defaults(func=cmd_doctor)

    config = sub.add_parser(
        "config", parents=[common], help="Generate PalWorldSettings.ini"
    )
    config.add_argument("--output", "-o")
    config.add_argument("--server-name")
    config.add_argument("--set", action="append", metavar="KEY=VALUE")
    config.set_defaults(func=cmd_config)

    inspect = sub.add_parser(
        "inspect", parents=[common], help="Read an existing PalWorldSettings.ini"
    )
    inspect.add_argument("path")
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    return parser


CLI_COMMANDS = ("doctor", "config", "inspect")


def _first_positional(argv: Sequence[str]) -> str | None:
    """First bare word in argv, skipping flags and their values.

    Needed because `--host X doctor` must still route to `doctor` rather than
    being mistaken for an agent action.
    """
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            # `--flag=value` carries its value; `--flag value` does not.
            if "=" not in token and token not in ("-h", "--help"):
                skip_next = True
            continue
        return token
    return None


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    command = _first_positional(argv)
    # `start`, `status`, `announce`, ... belong to the agent, not to this CLI.
    if command is not None and command not in CLI_COMMANDS:
        module = load_agent()
        return int(module.main(argv) or 0)

    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
