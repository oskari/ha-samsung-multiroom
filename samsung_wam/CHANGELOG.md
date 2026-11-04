# Changelog

## 0.2.1

- Speakers can go to standby again. When the app ends a speaker stream (after
  10 minutes paused, when the app stops, or when a bridge restarts), it now
  releases the speaker; before, the speaker kept waiting for the ended stream
  and stayed awake. It isn't sent when the speaker itself hangs up (e.g. after
  switching it to Bluetooth), so it can't pause anything else.
- When Spotify moves playback to another device, the speaker stream ends after
  1 minute instead of 10.
- Installs download a prebuilt image instead of building it on your machine.

## 0.2.0

- Choose per Spotify bridge how Spotify's volume slider works: it either
  scales the audio (speaker volume stays fixed, as before) or sets the
  speaker's own volume, up to a maximum you pick (faster, better quality).
- The speaker is only started when audio really plays. Before, a bridge that
  Spotify still showed as playing after a restart started the speaker on
  silence, which made it hang up and reconnect every few seconds.
- Silence during gaps is sent at real-time speed, so the speaker's buffer
  doesn't run dry.
- A speaker that keeps dropping the stream is retried with growing pauses
  (up to a minute) instead of immediately.
- Times in the panel follow your Home Assistant profile's time format
  (12/24-hour) instead of the browser's language.

## 0.1.0

First version.

- Sidebar panel: speakers, groups, Spotify bridges, new-speaker Wi-Fi setup.
- Spotify Connect bridges using Spotify Soloist, downloaded from Spotify and
  renewed before each build expires.
- Speakers on other networks (e.g. an IoT VLAN) can be added by network or by
  address.
