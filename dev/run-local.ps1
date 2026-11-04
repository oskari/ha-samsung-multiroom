# Dev environment 1: run the Home Assistant app's container directly in Docker
# Desktop, without Home Assistant. The UI is at http://localhost:8098 (no
# ingress, no Supervisor), speakers stream from <AdvertiseIp>:8099.
#
#   .\dev\run-local.ps1 -ExtraSubnet 192.168.10.0/24
#       build + run; also search that network (e.g. an IoT VLAN)
#   .\dev\run-local.ps1 -SeedBridge -SeedSpeakerIp 192.168.10.50
#       also add a bridge for that speaker, reusing a Soloist session paired
#       earlier (soloist-data-linux/): Docker Desktop can't do the pairing itself
#
# Needs SOLOIST_API_KEY in .env. The settings are kept in dev-data/ (git-ignored).
param(
  [string]$AdvertiseIp = "",    # this PC's LAN address the speakers can reach; detected if empty
  [string]$ExtraSubnet = "",
  [string]$SeedSpeakerIp = "",
  [switch]$SeedBridge
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$data = Join-Path $root "dev-data"
New-Item -ItemType Directory -Force $data | Out-Null
if (-not $AdvertiseIp) {
  $AdvertiseIp = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
    Select-Object -First 1).IPv4Address.IPAddress
  Write-Host "Speakers will stream from $AdvertiseIp (override with -AdvertiseIp)"
}
if ($SeedBridge -and -not $SeedSpeakerIp) { throw "-SeedBridge needs -SeedSpeakerIp" }

# Options as Supervisor would write them. The key is read from .env, never printed.
$keyLine = Get-Content (Join-Path $root ".env") | Where-Object { $_ -match '^\s*SOLOIST_API_KEY\s*=' } | Select-Object -First 1
$key = ($keyLine -replace '^\s*SOLOIST_API_KEY\s*=\s*', '').Trim().Trim('"').Trim("'")
@{ accept_spotify_terms = $true; soloist_api_key = $key; stream_port = 8099; advertise_ip = $AdvertiseIp } |
  ConvertTo-Json | Set-Content -Encoding utf8NoBOM (Join-Path $data "options.json")

$statePath = Join-Path $data "state.json"
if (-not (Test-Path $statePath)) {
  $state = @{ subnets = @($ExtraSubnet | Where-Object { $_ }); speakers = @{}; bridges = @() }
  if ($SeedBridge) {
    $id = "dev00001"
    $state.bridges = @(@{ id = $id; name = "Shape M3 Soloist"; speaker_mac = "";
                          speaker_ip = $SeedSpeakerIp; speaker_volume = $null })
    $dst = Join-Path $data "bridges\$id\data"
    New-Item -ItemType Directory -Force $dst | Out-Null
    Copy-Item -Recurse -Force (Join-Path $root "soloist-data-linux\*") $dst
  }
  $state | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8NoBOM $statePath
}

docker build -t samsung-wam-dev (Join-Path $root "samsung_wam")
if ($LASTEXITCODE) { exit $LASTEXITCODE }
docker rm -f samsung-wam-dev 2>$null | Out-Null
docker run -d --name samsung-wam-dev -p 8098:8098 -p 8099:8099 -e WAM_DEV=1 `
  -v "${data}:/data" samsung-wam-dev | Out-Null
Write-Host "Running. UI: http://localhost:8098   logs: docker logs -f samsung-wam-dev"
