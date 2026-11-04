# Dev environment 2: a throwaway Home Assistant with Supervisor (the official
# app-development container), with this repository mounted as local apps.
# Home Assistant: http://localhost:7123 (first start downloads Home Assistant
# Core and takes several minutes). Current Core builds serve on port 80 and only
# redirect from 8123, so 7123 maps to 80 (7124 maps to 8123 for older builds).
# The app appears under Settings > Apps > App store > Local apps. Run
# `python dev/ha_onboard.py` once to finish onboarding with a test account.
#
#   .\dev\run-ha.ps1          # start (or restart Supervisor)
#   .\dev\run-ha.ps1 -Remove  # delete the container and its volumes
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$name = "samsung-wam-ha-dev"

if ($Remove) {
  docker rm -f $name 2>$null | Out-Null
  docker volume rm "$name-docker" "$name-supervisor" 2>$null | Out-Null
  Write-Host "Removed."
  exit 0
}

if (-not (docker ps -q -f "name=^$name$")) {
  docker rm -f $name 2>$null | Out-Null
  docker run -d --name $name --privileged `
    -p 7123:80 -p 7124:8123 -p 7357:4357 `
    --mount "type=volume,source=$name-docker,target=/var/lib/docker" `
    --mount "type=volume,source=$name-supervisor,target=/mnt/supervisor" `
    -v "${root}:/mnt/supervisor/addons/local/samsung-wam-tools" `
    ghcr.io/home-assistant/devcontainer:2-addons sleep infinity | Out-Null
  docker exec $name bash -c "supervisor_bootstrap > /tmp/bootstrap.log 2>&1 || true"
}
# Current dev Supervisor builds keep Core's API sockets in the host's
# /run/supervisor, mounted into the Supervisor container at /run/os (as HA OS
# does). The devcontainer's script predates that, so run a patched copy.
docker exec $name sh -c "mkdir -p /run/supervisor && sed 's#-v /run/dbus:/run/dbus:ro#-v /run/dbus:/run/dbus:ro -v /run/supervisor:/run/os:rw#' /usr/bin/supervisor_run > /usr/local/bin/supervisor_run_shared && chmod +x /usr/local/bin/supervisor_run_shared"
# supervisor_run calls stty, so it needs a pseudo-terminal (-t) even when detached.
docker exec -dt $name bash -c "supervisor_run_shared > /tmp/supervisor.log 2>&1"
Write-Host "Starting. Home Assistant: http://localhost:7123  (log: docker exec $name tail -f /tmp/supervisor.log)"
