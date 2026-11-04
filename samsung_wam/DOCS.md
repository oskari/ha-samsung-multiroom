# Samsung Multiroom Speakers

Manage Samsung Wireless Audio Multiroom speakers (Shape M3/M5/M7, Radiant
R1–R7) from Home Assistant's sidebar, even though Samsung's app is abandoned
and Spotify dropped the speakers' built-in Spotify Connect.

- **Speakers**: find speakers (also on other networks such as an IoT VLAN),
  see their Wi-Fi network and signal, set the volume, move them to another
  Wi-Fi network.
- **Groups**: Samsung's own multiroom groups – the main speaker plays, the
  others follow in sync.
- **Spotify**: a Spotify Connect device per speaker or group, using
  [Spotify Soloist](https://developer.spotify.com/documentation/soloist),
  Spotify's official receiver. Pausing mutes the speaker immediately.
- **New speaker**: put a speaker in setup mode and connect it to your Wi-Fi
  through this host's own Wi-Fi adapter (when the host is on Ethernet and its
  Wi-Fi is unused).

## Sidebar

After installing, turn on **Show in sidebar** on the app's Info page. The
**Samsung Speakers** panel then appears in Home Assistant's sidebar (it's also
reachable with **Open web UI**).

## Spotify setup

1. You need Spotify Premium.
2. Create a Soloist API key at
   <https://developer.spotify.com/dashboard/soloist>.
3. In this app's **Configuration** tab, turn on **Accept Spotify's terms for
   Soloist** and paste the key. Restart the app. It downloads Soloist from
   Spotify and renews it before each build expires (every 90 days).
4. In the sidebar panel's **Spotify** tab, add a bridge for a speaker.
5. On a phone or computer on the same network, open Spotify, tap **Devices**
   and pick the new device once. It stays linked after that.

Audio reaches the speaker about 2–3 seconds after Spotify shows it.

## Networking

The speakers fetch audio from this host on the **stream port** (8099). If the
speakers are on a separate network (VLAN) from Home Assistant, allow TCP from
the speakers to this host on that port. Pairing needs the Spotify app on the
same network as this host (or mDNS forwarded between the networks). A VPN
client on the device used for pairing can make pairing hang.

## Notes

- Spotify Soloist is not part of this app; it is downloaded from Spotify on
  each install and used under Spotify's terms.
- Wi-Fi setup via the hotspot borrows the host's Wi-Fi adapter for about a
  minute and switches it off afterwards.
