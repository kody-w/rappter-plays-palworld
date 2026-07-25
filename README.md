# RAPPter Plays Palworld

An OpenRappter agent that inhabits a **self-hosted Palworld dedicated server**
through Palworld's official REST API. It watches every actor in the world,
reasons about what changed with GitHub Copilot, and speaks to players through
in-game announcements.

This is the sibling of [RAPPter Plays Pokémon](https://github.com/kody-w/rappter-plays-pokemon),
built as a stepping stone toward many autonomous agents sharing one persistent
3D world.

> [!IMPORTANT]
> This repository ships **no game files**. You supply your own Palworld
> dedicated server, installed from Steam (app `2394010`). The project never
> downloads, redistributes, or patches game binaries.

## What this actually is (read this first)

Palworld's official API is an **excellent sensor and a very limited actuator**.
That single fact shapes the whole design, so it is stated up front rather than
buried.

[`GET /game-data`](https://docs.palworldgame.com/api/rest-api/game-data/) returns
every actor in the world — players, owned pals, base pals, wild pals, NPCs,
PalBoxes — each with position, rotation, HP, level, guild, ownership links, and
the `Action` / `AI_Action` strings describing what it is doing right now. That
is a genuinely rich, officially supported world-state feed: the moral equivalent
of the emulator RAM map that makes the Pokémon agent cheap.

The write side is another story. The complete documented API is 12 endpoints:

| Capability | Endpoints |
|---|---|
| Read server state | `GET /info`, `GET /settings`, `GET /metrics` |
| Read world state | `GET /players`, `GET /game-data` |
| Moderation | `POST /kick`, `POST /ban`, `POST /unban` |
| Communication | `POST /announce` |
| Lifecycle | `POST /save`, `POST /shutdown`, `POST /stop` |

**Every write acts on the server or on a player's connection. None of them
mutate world state.** There is no documented endpoint that moves a character,
throws a Pal Sphere, places a building, or touches an inventory.

So the agent in this repo is a **warden, not a player**. It perceives
continuously and its only way to affect the world is to speak. That is a real,
useful autonomous agent — and it is honest about its limits.

In-world action requires a **UE4SS server-side mod**, which Pocketpair supports
[only on the Windows dedicated server](https://docs.palworldgame.com/settings-and-operation/mod/).
The agent defines an `Actuator` interface with a deliberately empty default so
that bridge can be dropped in later without reshaping the decision loop. See
[Roadmap](#roadmap).

## What you get

- **A RAPP agent, not a skill:** [`palworld_agent.py`](palworld_agent.py) holds
  the complete native `BasicAgent` contract, metadata, and `perform()`.
- **Full REST client:** [`restapi.py`](src/rappter_plays_palworld/restapi.py)
  covers all 12 documented endpoints with typed dataclasses, standard-library
  only, and precise error types (`PalworldAuthError`, `PalworldUnavailableError`).
- **World-state compression:**
  [`worldstate.py`](src/rappter_plays_palworld/worldstate.py) diffs consecutive
  snapshots into notable events and renders a bounded, model-facing digest.
  Wild-pal wandering is deliberately *not* an event; joins, deaths, big HP
  swings, level-ups and guild changes are.
- **Token discipline:** an unchanged world costs zero model calls. The brain is
  only consulted when the diff is non-empty.
- **Config generation:** builds and validates the notoriously fragile
  single-line `OptionSettings=(...)` blob, and parses existing ones back.
- **Windows provisioning:** [`provision-windows.ps1`](server/provision-windows.ps1)
  installs SteamCMD, downloads the server, performs first boot, writes an
  agent-ready config, and sets firewall rules that keep the REST API off the
  public internet.
- **102 tests**, fixtures built from the payload examples in the official docs.

## Requirements

**Server host — Windows strongly recommended**

| | |
|---|---|
| OS | Windows 64-bit (required for UE4SS mods later) or Linux 64-bit |
| CPU | 4+ cores |
| RAM | 16 GB minimum, 32 GB+ recommended |
| Disk | SSD required — Pocketpair warns slow storage corrupts saves |
| Ports | UDP `8211` (game), TCP `8212` (REST, LAN only) |

There is **no ARM64 build** of the dedicated server. The official image
`ghcr.io/pocketpairjp/palserver` is `linux/amd64` single-arch. Apple Silicon
cannot host it natively, and the emulation projects that exist publish no
benchmarks. Run the server on an x86-64 machine.

**Control plane** — macOS or Linux, Python 3.11+. It can be a different machine
from the server; it only needs LAN reach to the REST port.

## Setup

### 1. Provision the server (on the Windows host)

From an elevated PowerShell prompt:

```powershell
.\server\provision-windows.ps1 -AdminPassword '<a-long-random-secret>' -ServerName 'RAPPter World'
```

This installs SteamCMD, downloads app `2394010` (~8–10 GB), boots once to
create the config tree, writes `PalWorldSettings.ini` with `RESTAPIEnabled=True`,
and opens UDP 8211 publicly while restricting TCP 8212 to private/domain
network profiles.

Add `-PublicLobby` to list on the in-game community server browser — required
for Xbox and PS5 clients, which cannot enter an IP address directly.

Then start it:

```powershell
powershell -File C:\PalworldServer\start-server.ps1
```

### 2. Set up the control plane

```bash
git clone https://github.com/kody-w/rappter-plays-palworld
cd rappter-plays-palworld
./bootstrap.sh --setup-only
```

### 3. Point it at the server and verify

```bash
export PALWORLD_HOST=192.168.1.50        # your Windows host's LAN IP
export PALWORLD_ADMIN_PASSWORD='<the-same-secret>'

./launch.sh doctor
```

`doctor` checks reachability, credentials, metrics, and `/game-data` in order,
and tells you exactly what to fix when a step fails.

### 4. Bring the warden online

```bash
./launch.sh start --foreground
```

Add `--dry-run` to have it decide but never broadcast — useful for watching its
judgement before letting it speak to real players.

## Usage

```bash
./launch.sh world                 # full world digest right now
./launch.sh players               # connected players
./launch.sh metrics               # fps, frame time, uptime, bases, in-game day
./launch.sh announce --message "Server restarting in 5 minutes"
./launch.sh save
./launch.sh kick --userid steam_76561198000000000 --message "AFK"
./launch.sh shutdown --waittime 60 --message "Maintenance"

./launch.sh doctor                # verify the server is agent-ready
./launch.sh config -o out.ini     # generate PalWorldSettings.ini
./launch.sh inspect path/to/PalWorldSettings.ini
```

Config generation takes arbitrary overrides:

```bash
./launch.sh config --set ExpRate=2.0 --set DeathPenalty=None --set bIsPvP=true
```

## Configuration

Copy `config.example.json` and edit, or use environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `PALWORLD_HOST` | `127.0.0.1` | Server host or LAN IP |
| `PALWORLD_REST_PORT` | `8212` | `RESTAPIPort` |
| `PALWORLD_ADMIN_PASSWORD` | — | `AdminPassword`, used for Basic auth |

> [!NOTE]
> Pocketpair does not publish the REST API's default port or URL path prefix
> anywhere in the docs. `8212` and `/v1/api` come from the shipped
> `DefaultPalWorldSettings.ini` and observed behaviour. Both are overridable.

## Security

Pocketpair is explicit, on both API pages:

> These APIs are not designed to be exposed directly to the Internet.
> Publishing directly to the Internet may result in unauthorized manipulation
> of the server. It is recommended that they be used within the LAN.

Take that seriously. `AdminPassword` is the *only* credential guarding
kick/ban/shutdown, and Basic auth over plain HTTP sends it on every request. The
provisioning script therefore opens the REST port on private/domain firewall
profiles only. If you need remote access, tunnel it (WireGuard, Tailscale, SSH)
rather than forwarding the port.

RCON is **deprecated** and "scheduled to stop functioning in an upcoming
update". This project does not use it.

## Terms of service

Pocketpair's [EULA](https://eula.pocketpair.jp/palworld) §5.2.3 prohibits
"automation software (bots)" and §5.2.14 prohibits "any robot … automatic
device, process, software". Their
[mod guidelines](https://eula.pocketpair.jp/palworld-mod-guideline) reserve
enforcement for **official servers** and for "disruptive behavior" elsewhere.

This project targets a **private, self-hosted server**, uses only the official
REST API that Pocketpair ships for programmatic server access, and disrupts
nobody. Practical risk is low. There is, however, no written safe harbour — so
understand what you are doing before pointing this at a public server or
monetising it. Use at your own risk.

## Roadmap

1. **Perception + server ops** — *this release*. Official API only, no gray area.
2. **Public dashboard** — stream the world digest and the agent's reasoning to a
   GitHub Pages viewer, like the Pokémon story player.
3. **UE4SS actuation bridge** — a Windows-only server mod exposing movement and
   interaction behind the `Actuator` interface, so agents can genuinely play.
4. **Multi-agent** — many independent agents sharing one world, each with its
   own identity, goals, and memory.

Step 3 is the hard one. UE4SS gives you `RegisterHook`, `ExecuteInGameThread`
and arbitrary `UFunction` invocation — the *mechanism* — but Pocketpair ships no
modding SDK, so the real work is mapping Palworld's UObject graph to find the
right functions, against a build that changes with every patch.

## Development

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
```

The test suite runs without OpenRappter installed — `tests/test_palworld_agent.py`
stubs `openrappter.agents.basic_agent` so a bare checkout is testable.

## Reference

- [Palworld server docs](https://docs.palworldgame.com/)
- [REST API](https://docs.palworldgame.com/api/rest-api/palwold-rest-api/)
- [Configuration parameters](https://docs.palworldgame.com/settings-and-operation/configuration/)
- [Installing mods on a server](https://docs.palworldgame.com/settings-and-operation/mod/)
- [OpenRappter](https://github.com/kody-w/openrappter)

## License

MIT — see [LICENSE](LICENSE).

Palworld is a trademark of Pocketpair, Inc. This project is unaffiliated with
and unendorsed by Pocketpair.
