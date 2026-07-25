# Server host setup

Everything here runs on the **machine hosting the Palworld dedicated server**,
not on the control plane that runs the agent.

## Why Windows

Linux hosts the dedicated server perfectly well and the full REST API works, so
the perception half of this project runs fine on Linux. But Pocketpair states,
verbatim:

> At this time, server-side mods work only on the dedicated server with Windows
> edition.
> — <https://docs.palworldgame.com/settings-and-operation/mod/>

UE4SS server-side mods are the only path to agents *acting* in the world. Pick
Linux and you permanently cap this project at observe-and-speak. Pick Windows
and the actuation bridge stays possible.

## Hardware

| | Official guidance |
|---|---|
| CPU | `4 Core +（Recommended）` |
| Memory | `16GB` / `Recommended for larger than 32GB.` / `8GB is also bootable, but increases the possibility of server crashes due to out of memory.` |
| Storage | `Recommended for faster SSD.` / `Low-performance storage may corrupt saved data` |
| OS | `Windows 64bit` / `Linux 64bit (Ubuntu, AlmaLinux etc...)` |

Source: <https://docs.palworldgame.com/getting-started/requirements/>

There is **no ARM64 build**. The official Docker image is single-arch
`linux/amd64`. Apple Silicon cannot host this natively.

Budget ~10 GB of disk for the server payload, plus save growth. With
`bIsUseBackupSaveData=True` the server keeps a rolling backup set (5 per 30 s,
6 per 10 min, 12 per hour, 7 per day), so give it room.

## Provisioning

```powershell
# Elevated PowerShell
.\provision-windows.ps1 -AdminPassword '<a-long-random-secret>'
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `-InstallRoot` | `C:\PalworldServer` | Install location |
| `-AdminPassword` | *generated* | REST Basic-auth secret, 12+ chars |
| `-ServerName` | `RAPPter Plays Palworld` | Public name |
| `-GamePort` | `8211` | UDP game port |
| `-RestPort` | `8212` | TCP REST port |
| `-MaxPlayers` | `32` | Documented dedicated-server cap |
| `-PublicLobby` | off | List on the in-game community browser |
| `-SkipFirewall` | off | Leave firewall rules alone |

The script is idempotent: it skips SteamCMD download if present, skips first
boot if the config tree exists, and backs up any existing
`PalWorldSettings.ini` before overwriting.

## Ports

| Port | Protocol | Exposure |
|---|---|---|
| 8211 | UDP | **Public.** Forward on your router for remote players. |
| 8212 | TCP | **LAN only.** Never forward this. |

Pocketpair, on both API doc pages:

> These APIs are not designed to be exposed directly to the Internet.
> Publishing directly to the Internet may result in unauthorized manipulation
> of the server, which may interfere with play. It is recommended that they be
> used within the LAN.

The provisioning script enforces this: the REST rule is bound to the private
and domain firewall profiles only. For remote agent access, use a VPN
(WireGuard, Tailscale) or an SSH tunnel — not a port forward.

## Community server listing

Xbox and PS5 clients cannot enter an IP address. To let them join, run with
`-PublicLobby`, which adds `-publiclobby` to the launch line.

Caveat from the docs: if your router does not support Hairpin NAT, machines on
the *same* LAN cannot connect to your community server through its public
address.

## Config file

Live config lives at:

```
<InstallRoot>\PalServer\Pal\Saved\Config\WindowsServer\PalWorldSettings.ini
```

Editing `DefaultPalWorldSettings.ini` does nothing — it is a sample only.

Everything is one `OptionSettings=(...)` line under
`[/Script/Pal.PalGameWorldSettings]`. A single malformed value silently voids
the entire line, which is why the control plane can generate and validate it:

```bash
rappter-plays-palworld config --password '<secret>' -o PalWorldSettings.ini
rappter-plays-palworld inspect PalWorldSettings.ini
```

Restart the server after any config change.

## Performance notes

Pocketpair's guidance changed at v1.0. The `-useperfthreads
-NoAsyncLoadingThread -UseMultithreadForDS` flags that older guides recommend
now carry this note:

> In v1.0 and later, leaving this parameter unset may improve performance.

The generated launcher therefore omits them. Add them back only if you measure
an improvement via `GET /metrics` (`serverfps`, `serverframetime`).

Load-bearing settings worth knowing:

| Setting | Default | Note |
|---|---|---|
| `BaseCampMaxNum` | 128 | Hard world-wide base ceiling |
| `BaseCampMaxNumInGuild` | 4 | Max 10; raising it raises load |
| `BaseCampWorkerMaxNum` | 15 | Max 50; raising it raises load |
| `ServerReplicatePawnCullDistance` | 15000 | cm; range 5000–15000 |
| `ServerPlayerMaxNum` | 32 | Documented cap |

With defaults, 32 players × 4 bases each = exactly the 128 global cap.
