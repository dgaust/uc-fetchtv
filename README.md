# FetchTV for Unfolded Circle Remote

Control your [FetchTV](https://www.fetch.com.au/) set-top boxes from an
[Unfolded Circle Remote](https://www.unfoldedcircle.com/), see what each box is
playing, and pick channels from a browser that shows their logos.

The integration signs in to Fetch's own service with the same activation code
and PIN as the Fetch mobile app, and controls the boxes the way the app does.
It doesn't need Home Assistant or any other software, but it does need
internet access (see [Does it work locally?](#does-it-work-locally)).

> **Unofficial.** This project isn't made, endorsed or supported by Fetch, and
> Fetch's service isn't documented, so a change on their side can break it.
> "Fetch" and "FetchTV" are trademarks of their owner. See
> [Privacy and security](#privacy-and-security) and [License](#license).

## What you get

Every box on your Fetch account appears on the Remote as three entities:

**A media player** (named like the box)

- **What's playing**: the programme title from the Fetch guide (for
  free-to-air *and* subscription channels), the channel name and logo, the
  episode title, and how far through the programme you are.
- **On / off** (standby), **play/pause, stop, fast forward, rewind**, mute.
- **Navigation**: arrows, OK, back, menu, guide, info, exit, the four colour
  buttons, text, and the number pad.
- **Channels**: channel up/down, and a channel list to pick from, with your
  visible free-to-air channels and every subscription channel on your plan.
- **Channel browser**: open the Remote's media browser for **Free to air** and
  **Subscription** folders that list every channel with its logo and what is on
  now; pick one to tune the box.
- **Record** button.

**A channel select** ("<box name> channel")

Lists the channels with the current one selected, for dropdowns, UI pages and
activities: select a channel, next/previous, first/last.

**A remote** ("<box name> remote")

Every key the box accepts as a command (d-pad, guide, info, text, colours,
number pad, channel up/down), for activities and macros, with a default layout
for the Remote 3:

- *Buttons*: the d-pad, Back, the channel rocker, Mute, Menu (press for the
  guide, hold for the box's menu), Record, and the transport row (rewind,
  stop, play/pause, fast forward). Home and Power are left alone, and Back has
  no long press. The volume and Voice buttons aren't mapped, as a Fetch box has
  neither.
- *Pages*: **Numbers** (the number pad and Text) and **Keys** (the four colour
  keys, Info and Exit), which are the keys with no button.

The layout is only a starting point: change it in the web configurator.

Not supported (yet): programme posters (Fetch's service only provides channel
logos), browsing recordings. Free-to-air channels have no logo (Fetch doesn't
publish them), so they show a generic TV icon.

## What you need

- An Unfolded Circle Remote with internet access.
- Your Fetch **activation code** and **PIN** - the ones you use to sign in to
  the Fetch mobile app. The activation code is under My Account on
  [fetch.com.au](https://www.fetch.com.au/).

## Install it on the Remote

The driver runs on the Remote itself. No other hardware needed.

1. Download the latest `uc-intg-fetchtv-...-aarch64.tar.gz` file from the
   [Releases page](https://github.com/dgaust/uc-fetchtv/releases).
2. Open the Remote's web configurator in a browser. Go to
   **Integrations → Add new → Install custom** and upload the file.
3. Run the setup and type in your activation code and PIN.
4. Choose which of the boxes' entities to add.

Your boxes will appear, named the same as in the Fetch app.

### Updating

Do the same steps with the newer release file, but tick
**"Update existing driver"** when uploading. Your settings are kept, so you
won't need to set it up again. (This needs Remote firmware 2.9.3 or newer.)

**After an update that adds entities or features**, the Remote doesn't pick
them up for entities you already added, because it keeps each entity's details
from when it was added. Add the new entities in the web configurator, and
remove and re-add a media player or remote to get its new features or default
layout.

## Or run it in Docker

If you'd rather run the driver on an always-on computer, server or NAS, there
is a ready-made Docker image for Intel/AMD (x86) and ARM machines, including a
Raspberry Pi.

Save this as `docker-compose.yml` in a folder of its own:

```yaml
services:
  uc-fetchtv:
    image: ghcr.io/dgaust/uc-fetchtv:latest
    container_name: uc-fetchtv
    restart: unless-stopped
    # Required - see note below.
    network_mode: host
    environment:
      UC_INTEGRATION_HTTP_PORT: "10092"
    volumes:
      # Keeps your settings when the container restarts or updates.
      - ./config:/config
```

Then start it:

```bash
docker compose up -d
```

On the Remote, go to **Integrations → Add new**. The driver is found
automatically - select it from the list and type in your activation code and
PIN.

**Why `network_mode: host` is required:** the Remote finds the driver by
listening for announcements on the local network, and those announcements
can't get out of Docker's normal isolated networking.

The port is 10092, so it can run next to the
[Acmeda Pulse driver](https://github.com/dgaust/uc-acmeda-pulse) (10091) on the
same machine.

### Updating

```bash
docker compose pull && docker compose up -d
```

See the note above about entities the Remote already has.

### Using Cosmos?

In Cosmos, go to **ServApps → Start ServApp → Import Compose File**, paste the
contents of
[`cosmos-compose.json`](https://raw.githubusercontent.com/dgaust/uc-fetchtv/main/cosmos-compose.json)
and follow the installer.

### Settings (environment variables)

| Variable | Default | What it does |
| --- | --- | --- |
| `UC_INTEGRATION_HTTP_PORT` | `10092` (Docker) | The port the driver listens on. |
| `UC_INTEGRATION_INTERFACE` | all | The LAN address to listen on and announce, for example `192.168.1.50`. |
| `UC_CONFIG_HOME` | `/config` (Docker) | Where the account and box list are saved. |
| `UC_LOG_LEVEL` | `INFO` | `DEBUG` also logs every message the boxes send. Your activation code and PIN are never logged. |

## Troubleshooting

**The Remote can't find the driver** (Docker or Cosmos). The Remote finds it by
mDNS announcements. When the server has many network interfaces (typical with
Docker bridges), announcing on all of them can fail with "No buffer space
available" warnings in the log. Set `UC_INTEGRATION_INTERFACE` to the server's
LAN IP and recreate the container. Or, when adding the integration on the
Remote, open the advanced settings and enter the driver's address, for example
`ws://192.168.1.50:10092`.

**The boxes show as unavailable.** The Remote closes its connection to a driver
when it sleeps or when you disconnect the integration, and the driver then
drops its link to Fetch until the Remote reconnects. Wake the Remote or
reconnect the integration. If that doesn't help, check the driver's log for a
connection from the Remote.

**A new entity or feature is missing after an update.** See Updating above.

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
[docs/local-control-notes.md](docs/local-control-notes.md) for what a box
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
  other Unfolded Circle drivers. Run it only on a network you trust.

## For developers

### Run from source

```bash
cd intg-fetchtv
pip install -r requirements.txt
UC_INTEGRATION_HTTP_PORT=10092 python3 driver.py
```

Settings are saved to `config.json` in the folder set by the `UC_CONFIG_HOME`
environment variable (or the current folder if unset). The port comes from
`UC_INTEGRATION_HTTP_PORT` and is deliberately **not** set in `driver.json`: a
driver that reports a port in its metadata isn't reconnected to properly by
the Remote after it restarts.

### Tests

The tests in `tests/` are standalone scripts that run against a fake FetchTV
service (`tests/fake_fetch.py`) - run each one on its own:

```bash
pip install -r intg-fetchtv/requirements.txt
for t in tests/test_*.py; do python "$t"; done
```

They cover the FetchTV client (sign-in, box state, guide, commands,
reconnecting), the mapping of box state to what the Remote shows (media player,
channel browser and select), the remote entity (commands and default layout),
and the driver end to end over the Remote's websocket API. CI runs them on
every push.

### Releases (automated)

1. Change `"version"` in `intg-fetchtv/driver.json` (say `0.4.0`).
2. Commit and push, then tag it: `git tag v0.4.0 && git push origin v0.4.0`

CI then runs the tests, builds the install file for the Remote, and publishes
a **pre-release (beta)** on GitHub with the file and its checksum. Matching
Docker images are published at the same time. Once a beta has been tested on
a real Remote, untick "Set as a pre-release" on GitHub.

The install file can also be built locally with `bash build-intg.sh` (needs
Docker).

### What's in the repo

```
intg-fetchtv/
  driver.json      driver details + the setup screen (activation code, PIN)
  driver.py        starting point; handles connect/disconnect/standby
  setup_flow.py    first-time setup: signs in and finds the boxes
  box_manager.py   keeps the Fetch connection alive and updates the Remote
  fetchtv.py       talks to Fetch's service (sign-in, box messages, guide)
  entities.py      turns each box into a media player and a channel select
  remote_entity.py the remote entity: every key, default buttons and pages
  placeholder.py   the TV image shown for channels without a logo
  config.py        saves the account and box list
  shared.py        shared setup used by the other files
tests/             standalone test scripts + a fake FetchTV service
Dockerfile         Docker image for running on a server/NAS
docker-compose.yml ready-to-use Docker setup
cosmos-compose.json one-click install for Cosmos servers
LICENSE            Mozilla Public License 2.0
THIRD_PARTY_NOTICES.md  licences of what the driver is built with
```

### How it works (the short version)

- The driver signs in over HTTPS, then keeps one websocket open to
  `messages.fetchtv.com.au`. Boxes report their state over it (channel,
  title, live/paused, standby) as it changes, and are asked again every
  minute. Remote keys and channel changes go out over the same socket.
- Channel names and the programme guide come from Fetch's guide API. Subscription
  channels use their guide id as their channel id; free-to-air channel ids are
  regional variants of a guide id. Both are matched through the guide's channel
  list, so the programme shows on both.
- What is on now is prefetched for every channel in one request and topped up
  before it runs out, so the channel browser never waits on a lookup.
- The guide's own logo links point at a Fetch-internal host that nothing outside
  Fetch can load. Subscription channels' logos are served by Fetch's public
  site by guide key instead; free-to-air channels have none there, so they get
  an embedded Material-style TV icon at the same shape (266x150) as the logos.
- After every channel change a box also sends `PLAYBACK_STOPPED` (the old
  stream stopping). The driver ignores it as a state and asks the box for its
  real state instead.
- The driver remembers your account and box list, so after a restart your
  boxes are back straight away with no re-setup.
- The protocol isn't documented by Fetch. This project builds on what
  [pyfetchtv](https://github.com/jinxo13/pyfetchtv) worked out.
- The driver id is `fetchtv` and stays the same across versions, so the
  Remote treats a new upload as an update.

## License

Copyright (c) 2026 dgaust. Licensed under the
[Mozilla Public License 2.0](LICENSE) (MPL-2.0): you can use, change and share
it, and changes to its files must stay under the same licence. It comes with no
warranty.

The software and artwork it is built with keep their own licences; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The notices and `LICENSE` are
included in the install file and the Docker image.
