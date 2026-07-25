# RAPPter Plays Palworld

An autonomous agent that plays **Palworld through the user interface** — it
looks at the screen, decides, and presses keys, exactly like a person. No game
API, no console commands, no scripted macros.

This is the sequel to [RAPPter Plays Pokémon](https://github.com/kody-w/rappter-plays-pokemon).
Same loop, one rung harder: Pokémon Red proved it on a 2D emulator that waits
politely while the model thinks. Palworld is a real-time 3D world that does not
wait for anybody.

> [!IMPORTANT]
> This repository ships **no game files**. You supply your own Palworld client
> and dedicated server from Steam. The project never downloads, redistributes,
> or patches game binaries.

## Why Palworld is a serious benchmark

Agents that play 3D games are easy to demo and nearly impossible to *evaluate*.
The usual failure: you cannot tell whether the agent accomplished anything, so
you end up parsing pixels or — worse — trusting the agent's own narration. A
model that is eloquent about its progress scores like a model that makes some.

Palworld breaks that deadlock. It ships an official, server-authoritative
[`GET /game-data`](https://docs.palworldgame.com/api/rest-api/game-data/)
endpoint returning every actor in the world with exact position, HP, level,
guild, ownership, and current action. That gives the project two things almost
no other 3D target has:

1. **A ground-truth channel for the agent** — the equivalent of the cartridge
   RAM the Pokémon agent reads alongside its screenshots. The frame is what the
   agent *sees*; the oracle is what it *knows*. It stops the model inventing HP
   numbers off a blurry HUD.
2. **An independent scorer** — [`scoring.py`](src/rappter_plays_palworld/scoring.py)
   measures levels gained, pals owned, structures placed, ground covered,
   deaths, and survival, sampled straight from the server. **It never reads the
   agent's reasoning or self-reports.** That independence is the whole point;
   without it the benchmark grades its own homework.

The difficulty ladder is the real contribution. Pokémon Red is turn-based, 2D,
and fully observable. Palworld is real-time, 3D, partially observable, and
punishes latency. Same agent architecture, same `GameProfile` abstraction — the
loop does not know which game it is playing.

## The two rules that make real-time work

These are enforced in code, not hoped for, and they are where the Pokémon→
Palworld jump actually bites:

**Held keys are released before every decision.** A model call takes seconds.
If the agent is holding `w` when it stops to think, the character sprints into
the ocean. Movement is expressed as bounded, self-terminating bursts, and every
failure path calls `release_all()`.

**Plans are capped at 6 seconds.** A stale plan executed against a changed world
is worse than no plan. Long plans get truncated rather than trusted.

## Architecture

```
   screen  ──►  capture.py   ──┐
                               ├──►  brain (Copilot, vision)  ──►  plan
   server  ──►  restapi.py   ──┘                                     │
                    │                                                ▼
                    │                                         inputs.py
                    │                                     (scancode SendInput)
                    ▼                                                │
               scoring.py  ◄──────── independent ────────────────────┘
```

| Module | Role |
|---|---|
| [`capture.py`](src/rappter_plays_palworld/capture.py) | Grabs the game window via `GetClientRect`/`ClientToScreen`, so window chrome, notifications and second monitors never reach the model |
| [`inputs.py`](src/rappter_plays_palworld/inputs.py) | Synthetic keyboard/mouse through `SendInput` |
| [`profiles.py`](src/rappter_plays_palworld/profiles.py) | Per-game keybinds, action vocabulary, goal. **Adding a game is adding a profile** |
| [`gameplay.py`](src/rappter_plays_palworld/gameplay.py) | The loop: capture → prompt → plan → keystrokes |
| [`restapi.py`](src/rappter_plays_palworld/restapi.py) | All 12 documented REST endpoints, stdlib-only, typed |
| [`scoring.py`](src/rappter_plays_palworld/scoring.py) | Objective run scoring from the oracle alone |
| [`worldstate.py`](src/rappter_plays_palworld/worldstate.py) | Snapshot diffing and bounded digests |

### Two details that decide whether input works at all

**Scancodes, not virtual keys.** Palworld is an Unreal Engine DirectX title.
DirectInput-style games read hardware scancodes and routinely discard
`SendInput` events carrying only a virtual-key code. Sending `VK_W` does
nothing; sending scancode `0x11` with `KEYEVENTF_SCANCODE` walks forward.

**Relative mouse motion.** A 3D camera is driven by *deltas*. Warping the cursor
to an absolute screen position does not turn the camera. Deltas are also split
into small increments, because one large jump reads as a teleport to the
engine's input smoothing and gets partially discarded.

## Requirements

**The agent must run on the machine hosting the game.** The Palworld client is
Windows-only — Steam reports `mac: false`, and synthetic input cannot cross a
remote-desktop boundary into another machine's game.

| | |
|---|---|
| OS | Windows 64-bit |
| RAM | 16 GB for the client, 16 GB more if the server is on the same box |
| GPU | Palworld recommends an RTX 3060 Ti class card |
| Python | 3.11+ |

The dedicated server can live on the same machine or another one. There is
**no ARM64 build** of the server — the official image is `linux/amd64`
single-arch, so Apple Silicon cannot host it.

## Setup

### 1. Provision the dedicated server

From an elevated PowerShell prompt on the server host:

```powershell
.\server\provision-windows.ps1 -AdminPassword '<a-long-random-secret>'
```

Installs SteamCMD, downloads app `2394010` (~8–10 GB), boots once to create the
config tree, enables the REST API, and opens UDP 8211 publicly while keeping TCP
8212 on private firewall profiles only.

### 2. Install the agent

```bash
git clone https://github.com/kody-w/rappter-plays-palworld
cd rappter-plays-palworld
./bootstrap.sh --setup-only
```

### 3. Verify the oracle

```bash
export PALWORLD_HOST=127.0.0.1
export PALWORLD_ADMIN_PASSWORD='<the-same-secret>'
./launch.sh doctor
```

### 4. Play

Start Palworld, join your server, then — with the game visible:

```bash
./launch.sh play --dry-run        # decides, sends no input
./launch.sh play                  # actually plays
./launch.sh status
./launch.sh stop
```

**Always start with `--dry-run`.** It captures the window and reasons about it
without touching your keyboard, so you can read the agent's judgement before it
gets the controls.

## Usage

```bash
./launch.sh play --player-name Kody   # anchor ground truth to your character
./launch.sh world                     # world digest right now
./launch.sh players
./launch.sh metrics
./launch.sh doctor
./launch.sh config -o PalWorldSettings.ini
```

There is also a **warden** mode (`./launch.sh start`) — a server-side agent that
watches the world and speaks through `/announce` without playing. It uses only
the official API and is useful for moderation and ambience, but it is not the
point of this project.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PALWORLD_HOST` | `127.0.0.1` | Server host |
| `PALWORLD_REST_PORT` | `8212` | `RESTAPIPort` |
| `PALWORLD_ADMIN_PASSWORD` | — | `AdminPassword`, Basic-auth secret |

> [!NOTE]
> Pocketpair does not publish the REST API's default port or URL path prefix.
> `8212` and `/v1/api` come from the shipped `DefaultPalWorldSettings.ini` and
> observed behaviour. Both are overridable.

## Security

Pocketpair, on both API doc pages:

> These APIs are not designed to be exposed directly to the Internet. It is
> recommended that they be used within the LAN.

`AdminPassword` is the only credential guarding kick/ban/shutdown, and Basic
auth over plain HTTP sends it on every request. The provisioning script keeps
the REST port on private/domain firewall profiles. Tunnel it (WireGuard,
Tailscale, SSH) rather than forwarding the port.

RCON is deprecated and "scheduled to stop functioning in an upcoming update".
This project does not use it.

## Terms of service

Pocketpair's [EULA](https://eula.pocketpair.jp/palworld) §5.2.3 prohibits
"automation software (bots)" and §5.2.14 "any robot … automatic device, process,
software". Their [mod guidelines](https://eula.pocketpair.jp/palworld-mod-guideline)
reserve enforcement for **official servers** and for "disruptive behavior"
elsewhere.

This project targets a **private, self-hosted server** and disrupts nobody, so
practical risk is low. But the EULA carves out no safe harbour for research or
private play — read those clauses and decide for yourself before pointing this
at anything public or commercial. Use at your own risk.

## Roadmap

1. **Play loop + objective scoring** — *this release*
2. **Live dashboard** — stream frames, reasoning, and the running score card
3. **Multi-agent** — several agents in one world, each with its own identity,
   goals, and memory, plus other people's agents joining
4. **More profiles** — the loop is game-agnostic; the benchmark grows by adding
   `GameProfile`s, not by rewriting the agent

## Development

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q      # 201 tests
.venv/bin/ruff check .
```

The suite runs on macOS and Linux without a game, a server, or OpenRappter
installed: input is exercised through a recording backend, and
`tests/test_palworld_agent.py` stubs `openrappter.agents.basic_agent`.

## Reference

- [Palworld server docs](https://docs.palworldgame.com/)
- [REST API](https://docs.palworldgame.com/api/rest-api/palwold-rest-api/)
- [Configuration parameters](https://docs.palworldgame.com/settings-and-operation/configuration/)
- [OpenRappter](https://github.com/kody-w/openrappter)

## License

MIT — see [LICENSE](LICENSE).

Palworld is a trademark of Pocketpair, Inc. This project is unaffiliated with
and unendorsed by Pocketpair.
