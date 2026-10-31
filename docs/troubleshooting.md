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

## Logs and debugging

At the default level (`INFO`) the log shows start-up, sign-in, the connection to
Fetch, and boxes appearing, going quiet or changing standby. With debug logging
on it also shows every message to and from each box, every Fetch request (the
path and how long it took), every command from the Remote with its outcome, and
what is pushed to the Remote. Switch it on, reproduce the problem, then read the
log.

**On the Remote.** The Remote has no log level setting for a driver; it stores
what a driver prints and lets you download it. So the driver has its own switch:
run the FetchTV setup again from **Integrations** in the web configurator,
re-enter the code and PIN, and tick **Debug logging**. Untick it the same way
afterwards. Download the log from **Settings → Development: Logs** in the web
configurator. On firmware 2.1.0 and newer there is also a live log viewer at
`http://<remote address>/log/`, which is off by default and needs a request to
the Remote's `/api/system/logs/web` endpoint to start; see Unfolded Circle's
[driver installation notes](https://github.com/unfoldedcircle/core-api/blob/main/doc/integration-driver/driver-installation.md)
(section "Log access").

**In Docker.** Set `UC_LOG_LEVEL` to `DEBUG` in the compose file, run
`docker compose up -d`, and read the log with `docker logs -f uc-fetchtv` (add
`--since 10m` for the last ten minutes).

**In Cosmos Cloud.** Open the container's **Docker** tab, set the environment
variable `UC_LOG_LEVEL` to `DEBUG` (add it if it isn't listed) and press **Update
Container**, then read the **Logs** tab. A container update or recreate clears
that tab, so reproduce the problem after switching to `DEBUG`.

`UC_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING` or `ERROR`) always wins over the tick
box. The first line of the log says which level is in use and where it came from.

**What to look for**

| In the log | Means |
| --- | --- |
| `Signed in to FetchTV: 2 box(es)` | The activation code and PIN work and the account has two boxes |
| `FetchTV sign-in rejected` | Fetch refused the code or PIN: run the setup again with the right ones |
| `Connected to FetchTV's message service` | The link to Fetch is up |
| `<box> is reachable (first answer: ...)` | The box answered; it should show as available |
| `No message from <box> for 300 s ...` | The link is up but that box says nothing: it may be off the network, or its own link to Fetch has dropped (retrying in the Fetch app brings it back) |
| `<box> is answering again after ... s` | It came back |
| `<box>: in standby` / `awake` | The box changed power state |
| `FetchTV connection lost` then `Reconnecting to FetchTV in ...` | The link dropped; the driver signs in again by itself, waiting longer after repeated failures |
| `Remote went away: keeping the FetchTV session` | The Remote slept or disconnected; the session stays for the grace period |
| `Power off asked of <box> while its state is unknown: refused` | A power command was held back because the box hasn't reported its state |

With debug on, `<box> -> PING`, `-> ARE_YOU_ALIVE` and `-> MEDIA_STATE` are what
the driver asks each box, and `<box> <- PONG`, `<- I_AM_ALIVE`, `<- MEDIA_STATE`
are the answers. A box that is asked and never answers is a problem on Fetch's
side or the box's, not the Remote's.

**Before you share a log.** Your activation code, PIN, sign-in token and anything
typed into a box are never written to it. It does contain your box names and
their terminal ids (which are their MAC addresses), what was playing, and your
recordings' titles, so look it over first.

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
