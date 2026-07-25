<#
.SYNOPSIS
    Provision a Palworld dedicated server on Windows for RAPPter Plays Palworld.

.DESCRIPTION
    Installs SteamCMD, downloads the Palworld dedicated server (Steam app
    2394010), performs the first boot so the config tree is created, writes an
    agent-ready PalWorldSettings.ini, and opens the required firewall ports.

    Windows is required -- not preferred. Pocketpair documents that
    "server-side mods work only on the dedicated server with Windows edition",
    so a Windows host is the only one where a UE4SS agent bridge can ever run.

.PARAMETER InstallRoot
    Where SteamCMD and the server are installed. Default C:\PalworldServer.

.PARAMETER AdminPassword
    REST API Basic-auth password. Required, minimum 12 characters. If omitted a
    strong one is generated and printed once.

.PARAMETER ServerName
    Public server name.

.PARAMETER GamePort
    UDP game port. Default 8211.

.PARAMETER RestPort
    TCP REST API port. Default 8212. Bound LAN-side only, see notes.

.PARAMETER PublicLobby
    Register on the in-game community server list. Required for Xbox/PS5
    clients, which cannot enter an IP address directly.

.EXAMPLE
    .\provision-windows.ps1 -AdminPassword 'a-long-random-secret' -ServerName 'RAPPter World'

.NOTES
    Run from an elevated PowerShell prompt.
    Requirements: 64-bit Windows, 16GB RAM minimum (32GB+ recommended), SSD.
    Pocketpair warns that low-performance storage can corrupt save data.
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\PalworldServer',
    [string]$AdminPassword,
    [string]$ServerName = 'RAPPter Plays Palworld',
    [string]$ServerDescription = 'Autonomous agents on a self-hosted world.',
    [int]$GamePort = 8211,
    [int]$RestPort = 8212,
    [int]$MaxPlayers = 32,
    [switch]$PublicLobby,
    [switch]$SkipFirewall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$PALWORLD_APP_ID = 2394010
$STEAMCMD_URL = 'https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip'

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn { param([string]$Message) Write-Host "!!  $Message" -ForegroundColor Yellow }

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script must be run from an elevated PowerShell prompt.'
    }
}

function New-StrongPassword {
    $bytes = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('/', '_').Replace('+', '-')
}

function Test-Hardware {
    $ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
    $cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum
    Write-Host "    Detected ${ram}GB RAM, ${cores} physical cores."
    if ($ram -lt 16) {
        Write-Warn "Palworld documents 16GB minimum. ${ram}GB will risk out-of-memory crashes."
    }
    if ($cores -lt 4) {
        Write-Warn "Palworld recommends 4+ cores. Detected ${cores}."
    }
}

# ---------------------------------------------------------------------------

Assert-Administrator
Test-Hardware

if (-not $AdminPassword) {
    $AdminPassword = New-StrongPassword
    Write-Warn 'No -AdminPassword supplied. Generated one:'
    Write-Host ''
    Write-Host "    $AdminPassword" -ForegroundColor Green
    Write-Host ''
    Write-Warn 'Save it now. It is the only credential guarding kick/ban/shutdown.'
}
if ($AdminPassword.Length -lt 12) {
    throw 'AdminPassword must be at least 12 characters.'
}

$steamCmdDir = Join-Path $InstallRoot 'steamcmd'
$serverDir = Join-Path $InstallRoot 'PalServer'
$steamCmdExe = Join-Path $steamCmdDir 'steamcmd.exe'

Write-Step "Creating $InstallRoot"
New-Item -ItemType Directory -Force -Path $steamCmdDir | Out-Null

if (-not (Test-Path $steamCmdExe)) {
    Write-Step 'Downloading SteamCMD'
    $zipPath = Join-Path $env:TEMP 'steamcmd.zip'
    Invoke-WebRequest -Uri $STEAMCMD_URL -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $steamCmdDir -Force
    Remove-Item $zipPath -Force
} else {
    Write-Step 'SteamCMD already present'
}

Write-Step "Downloading Palworld dedicated server (app $PALWORLD_APP_ID)"
Write-Host '    This is roughly 8-10GB and may take a while.'
& $steamCmdExe +force_install_dir $serverDir +login anonymous `
    +app_update $PALWORLD_APP_ID validate +quit
if ($LASTEXITCODE -ne 0) {
    throw "SteamCMD exited with code $LASTEXITCODE"
}

$serverExe = Join-Path $serverDir 'PalServer.exe'
if (-not (Test-Path $serverExe)) {
    throw "PalServer.exe not found at $serverExe -- the download did not complete."
}

# The Pal\Saved tree only exists after the server has run once. Boot it, wait
# for the config directory to appear, then stop it.
$configDir = Join-Path $serverDir 'Pal\Saved\Config\WindowsServer'
if (-not (Test-Path $configDir)) {
    Write-Step 'First boot to generate the config tree'
    $process = Start-Process -FilePath $serverExe -PassThru -WindowStyle Minimized
    $deadline = (Get-Date).AddMinutes(5)
    while (-not (Test-Path $configDir) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
    }
    Write-Step 'Stopping first-boot server'
    if (-not $process.HasExited) {
        $process.Kill()
        $process.WaitForExit(30000) | Out-Null
    }
    if (-not (Test-Path $configDir)) {
        throw "Config directory was not created at $configDir after 5 minutes."
    }
} else {
    Write-Step 'Config tree already exists'
}

Write-Step 'Writing agent-ready PalWorldSettings.ini'
$settingsPath = Join-Path $configDir 'PalWorldSettings.ini'
if (Test-Path $settingsPath) {
    $backup = "$settingsPath.bak"
    Copy-Item $settingsPath $backup -Force
    Write-Host "    Backed up existing config to $backup"
}

# Escape any embedded quotes so a password containing " cannot break the line.
$escapedPassword = $AdminPassword.Replace('"', '\"')
$escapedName = $ServerName.Replace('"', '\"')
$escapedDescription = $ServerDescription.Replace('"', '\"')

$optionPairs = @(
    "ServerName=`"$escapedName`""
    "ServerDescription=`"$escapedDescription`""
    "AdminPassword=`"$escapedPassword`""
    "ServerPlayerMaxNum=$MaxPlayers"
    'RESTAPIEnabled=True'
    "RESTAPIPort=$RestPort"
    'RCONEnabled=False'
    'bShowPlayerList=True'
    'bIsShowJoinLeftMessage=True'
    'bExistPlayerAfterLogout=False'
    'bIsUseBackupSaveData=True'
    'LogFormatType=Json'
    'ChatPostLimitPerMinute=60'
    'BaseCampMaxNumInGuild=4'
    'BaseCampMaxNum=128'
) -join ','

$iniBody = "[/Script/Pal.PalGameWorldSettings]`r`nOptionSettings=($optionPairs)`r`n"
Set-Content -Path $settingsPath -Value $iniBody -Encoding UTF8 -NoNewline
Write-Host "    Wrote $settingsPath"

if (-not $SkipFirewall) {
    Write-Step 'Configuring Windows Firewall'

    # The game port must be reachable from the internet for remote players.
    Remove-NetFirewallRule -DisplayName 'Palworld Server (UDP)' -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName 'Palworld Server (UDP)' -Direction Inbound `
        -Protocol UDP -LocalPort $GamePort -Action Allow -Profile Any | Out-Null
    Write-Host "    Allowed inbound UDP $GamePort (game traffic)"

    # The REST API is deliberately NOT opened to the internet. Pocketpair:
    # "These APIs are not designed to be exposed directly to the Internet ...
    # It is recommended that they be used within the LAN."
    Remove-NetFirewallRule -DisplayName 'Palworld REST API (LAN only)' -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName 'Palworld REST API (LAN only)' -Direction Inbound `
        -Protocol TCP -LocalPort $RestPort -Action Allow -Profile Private, Domain | Out-Null
    Write-Host "    Allowed inbound TCP $RestPort on private/domain profiles only"
    Write-Warn "The REST API is intentionally NOT exposed to the public profile."
}

# ---------------------------------------------------------------------------

$launchArgs = @("-port=$GamePort", "-players=$MaxPlayers")
if ($PublicLobby) { $launchArgs += '-publiclobby' }

$startScript = Join-Path $InstallRoot 'start-server.ps1'
$startBody = @"
# Generated by provision-windows.ps1
# Note: Pocketpair states that in v1.0+, leaving the -useperfthreads /
# -NoAsyncLoadingThread / -UseMultithreadForDS flags UNSET may improve
# performance. They are omitted here deliberately.
Set-Location '$serverDir'
& '$serverExe' $($launchArgs -join ' ')
"@
Set-Content -Path $startScript -Value $startBody -Encoding UTF8
Write-Step "Wrote launcher: $startScript"

Write-Host ''
Write-Host 'Provisioning complete.' -ForegroundColor Green
Write-Host ''
Write-Host "  Server dir     $serverDir"
Write-Host "  Config         $settingsPath"
Write-Host "  Start with     powershell -File `"$startScript`""
Write-Host "  Game port      UDP $GamePort  (forward this on your router)"
Write-Host "  REST API       http://<lan-ip>:$RestPort/v1/api  (LAN only)"
Write-Host ''
Write-Host 'Point the agent at it:' -ForegroundColor Cyan
Write-Host "  rappter-plays-palworld start --host <lan-ip> --rest-port $RestPort"
Write-Host ''
if (-not $PublicLobby) {
    Write-Host 'Re-run with -PublicLobby to list on the in-game community server browser'
    Write-Host '(required for Xbox and PS5 clients, which cannot enter an IP directly).'
}
