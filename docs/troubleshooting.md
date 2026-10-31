# Troubleshooting and notes

## Troubleshooting

**The Remote can't find the driver** (Docker or Cosmos). The Remote finds it by
mDNS announcements. When the server has many network interfaces (typical with
Docker bridges), announcing on all of them can fail with "No buffer space
available" warnings in the log. Set `UC_INTEGRATION_INTERFACE` to the server's
LAN IP and recreate the container. Or, when adding the integration on the
Remote, open the advanced settings and enter the driver's address, for example
`ws://192.168.1.50:10092`.

**A box shows as unavailable.** The driver has not heard from that box through
Fetch's service. Most often the box's own connection to Fetch has dropped while
the box is still on your network (the Fetch app shows the same box as
unreachable until you tap retry, and the driver picks the box up again as soon
as it answers). Retrying from the Fetch app, or restarting the box, brings it
back. While a box is unavailable, off and toggle are refused rather than
guessed (the box only has a power toggle, so a blind press could switch it the
wrong way), and on sends a wake, which never switches a box off.

Since 0.4.1 the driver introduces itself to Fetch the way the Fetch Android app
does (its headers, a per-install device id, the token in the websocket URL).
If a box never answers although the Fetch app controls it, set
`UC_FETCH_LEGACY_HANDSHAKE=1` and restart the driver to connect the way earlier
versions did, and compare. The log says when it is on.

When the Remote sleeps or you disconnect the integration, the driver keeps its
link to Fetch for 10 minutes before closing it, so a Remote that comes back in
that time does not sign in again. If every box is unavailable, check the
driver's log for a connection from the Remote.

**A new entity or feature is missing after an update.** After an update that adds entities, add them in the Remote's web configurator, and re-add a media player or remote to get new features (see "To update" in the [README](../README.md)).

**Changing channel with the box's own IR remote doesn't show straight away.**
The driver learns of changes from what the box reports to Fetch's service. That
can lag behind a change made with the box's own remote.

## Does it work locally?

No. The driver never connects to your boxes. It talks to Fetch's servers
(`apis.fetchtv.com.au` and `messages.fetchtv.com.au`, which are hosted in
Sydney), the same way the Fetch mobile app does, and Fetch's service relays
commands to the box. It feels instant because the boxes keep a connection open
to Fetch and the servers are close: connecting to them takes about 30 ms from
Australian broadband. It also means the driver needs internet access, and that
it stops working if Fetch's service is down. Whether a box can be controlled
directly on your network is an open question: see
[docs/local-control-notes.md](local-control-notes.md) for what a box
exposes locally.

## Privacy and security

- Your **activation code and PIN** are saved in `config.json` in the driver's
  config folder (on the Remote, or the Docker volume) so it can sign in again
  after a restart. They are stored as plain text, so keep that folder private.
  They are never written to the logs.
- The driver contacts **only Fetch**: `apis.fetchtv.com.au` and
  `messages.fetchtv.com.au` for sign-in, control and the guide. The Remote
  itself loads channel logos from `www.fetchtv.com.au`. There is no analytics
  or other third party.
- The driver's websocket on the Remote's own network has no password, like
  other Unfolded Circle drivers. Anyone who can reach its port can send keys
  to your boxes, type text, play things, and even run the setup again with
  another account. Run it only on a network you trust, and with Docker set
  `UC_INTEGRATION_INTERFACE` to the server's LAN address so it listens only
  there.
