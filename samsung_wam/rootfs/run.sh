#!/bin/sh
# Private PulseAudio for the Spotify bridges, then the app.
set -e
mkdir -p "$XDG_RUNTIME_DIR/pulse"
chmod 700 "$XDG_RUNTIME_DIR"

# Runs as root inside the container; PulseAudio warns about that but works.
pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit -n \
  --load="module-native-protocol-unix socket=$XDG_RUNTIME_DIR/pulse/native auth-anonymous=1" \
  --load="module-null-sink sink_name=wam_idle sink_properties=device.description=idle" \
  --log-target=stderr --log-level=error 2>/dev/null || true

i=0
until pactl info >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 100 ]; then echo "PulseAudio did not start" >&2; exit 1; fi
  sleep 0.1
done
pactl set-default-sink wam_idle

exec python3 -m wam
