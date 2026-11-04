# Demo environment: four fake speakers (dev/fake_speaker.py) on a private
# Docker network, and a copy of the app that only sees them. Good for
# screenshots and for trying groups without owning several speakers.
# UI: http://localhost:8097
#
#   .\dev\run-demo.ps1          # start (rebuilds the app image)
#   .\dev\run-demo.ps1 -Remove  # stop and remove everything it started
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$net = "wam-demo"
$speakers = @(
  @{ name = "wam-demo-living"; ip = "172.28.5.11"; args = @("--name", "Living room", "--model", "SPK-WAM750", "--mac", "02:00:00:00:00:11", "--volume", "12", "--rssi", "5", "--group", "Downstairs") },
  @{ name = "wam-demo-kitchen"; ip = "172.28.5.12"; args = @("--name", "Kitchen", "--model", "SPK-WAM550", "--mac", "02:00:00:00:00:12", "--volume", "9", "--rssi", "4", "--main-ip", "172.28.5.11") },
  @{ name = "wam-demo-bedroom"; ip = "172.28.5.13"; args = @("--name", "Bedroom", "--model", "SPK-WAM350", "--mac", "02:00:00:00:00:13", "--volume", "6", "--rssi", "3", "--ch", "11") },
  @{ name = "wam-demo-office"; ip = "172.28.5.14"; args = @("--name", "Office", "--model", "SPK-WAM1500", "--mac", "02:00:00:00:00:14", "--volume", "7", "--rssi", "4", "--ch", "1") }
)

foreach ($c in @("wam-demo-app") + ($speakers | ForEach-Object { $_.name })) { docker rm -f $c 2>$null | Out-Null }
if ($Remove) { docker network rm $net 2>$null | Out-Null; Write-Host "Removed."; exit 0 }

if (-not (docker network ls -q -f "name=^$net$")) { docker network create --subnet 172.28.5.0/24 $net | Out-Null }
docker build -q -t samsung-wam-dev (Join-Path $root "samsung_wam") | Out-Null

foreach ($s in $speakers) {
  docker run -d --name $s.name --network $net --ip $s.ip -v "${PSScriptRoot}:/devtools:ro" `
    --entrypoint python3 samsung-wam-dev /devtools/fake_speaker.py --ip $s.ip @($s.args) | Out-Null
}

$data = Join-Path $root "dev-data-demo"
New-Item -ItemType Directory -Force $data | Out-Null
@{ accept_spotify_terms = $false; soloist_api_key = ""; stream_port = 8099 } |
  ConvertTo-Json | Set-Content -Encoding utf8NoBOM (Join-Path $data "options.json")
docker run -d --name wam-demo-app --network $net --ip 172.28.5.2 -p 8097:8098 -e WAM_DEV=1 `
  -v "${data}:/data" samsung-wam-dev | Out-Null
Write-Host "Demo running. UI: http://localhost:8097   (remove with -Remove)"
