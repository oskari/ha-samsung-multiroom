#!/bin/sh
# Smoke test for a built app image, run inside the image without starting the
# app (so nothing is downloaded and no network access is needed):
#
#   docker run --rm -i --entrypoint /bin/sh <image> -s < .github/scripts/smoke-test.sh
set -eu

python3 -c 'import wam.__main__, wam.bridge, wam.discovery, wam.groups, wam.protocol, wam.soloist, wam.state, wam.web, wam.wifi'
echo "app modules import with $(python3 --version)"
pulseaudio --version
pactl --version | head -n 1
test -x /run.sh

# Spotify Soloist may not be redistributed: the app downloads it from Spotify
# at runtime, into /data, after the user accepts Spotify's terms.
if find / -xdev \( -name soloist -o -name 'soloist_release*' \) -print | grep .; then
  echo "Spotify Soloist must not be part of the image" >&2
  exit 1
fi
echo "no Spotify Soloist in the image"
