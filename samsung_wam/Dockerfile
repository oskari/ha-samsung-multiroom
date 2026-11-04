# Soloist needs glibc, so this app uses Debian rather than Home Assistant's
# Alpine base image (Supervisor's BUILD_FROM argument is deliberately unused).
FROM docker.io/library/debian:trixie-slim

# PulseAudio provides one virtual output per Spotify bridge; parec records it.
# Spotify Soloist itself is downloaded from Spotify at runtime (it may not be
# redistributed), after the user accepts Spotify's terms in the app options.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      pulseaudio pulseaudio-utils libpulse0 libatomic1 \
      python3 ca-certificates iproute2 tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY rootfs/ /
RUN chmod +x /run.sh

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app \
    XDG_RUNTIME_DIR=/tmp/xdg PULSE_SERVER=unix:/tmp/xdg/pulse/native

CMD ["/run.sh"]
