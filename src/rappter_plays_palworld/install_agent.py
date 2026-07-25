"""Atomically register this repository's single-file agent with OpenRappter."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
from pathlib import Path
from typing import Sequence

AGENT_FILENAME = "palworld_agent.py"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def openrappter_agents_dir() -> Path:
    try:
        agents = importlib.import_module("openrappter.agents")
    except ImportError as error:
        raise RuntimeError(
            "OpenRappter is not installed in this Python environment"
        ) from error
    package_file = getattr(agents, "__file__", None)
    if not package_file:
        raise RuntimeError("Cannot locate the OpenRappter agents package")
    return Path(package_file).resolve().parent


def install_agent(source: Path, destination_dir: Path | None = None) -> Path:
    source = source.expanduser().resolve()
    if not source.is_file() or source.name != AGENT_FILENAME:
        raise RuntimeError(f"Agent source does not exist: {source}")
    destination_dir = (destination_dir or openrappter_agents_dir()).resolve()
    if not destination_dir.is_dir():
        raise RuntimeError(f"Agent destination does not exist: {destination_dir}")
    destination = destination_dir / AGENT_FILENAME
    if destination.exists() and sha256(destination) == sha256(source):
        return destination

    temporary = destination.with_name(f".{AGENT_FILENAME}.{os.getpid()}.tmp")
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as output:
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    importlib.invalidate_caches()
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Register {AGENT_FILENAME} in the active OpenRappter install"
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = install_agent(args.source, args.destination_dir)
    except RuntimeError as error:
        print(f"error: {error}")
        return 1
    print(f"Registered Palworld RAPP agent: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
