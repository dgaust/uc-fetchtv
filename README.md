# FetchTV for Unfolded Circle Remote

Control your [FetchTV](https://www.fetch.com.au/) set-top boxes from an
[Unfolded Circle Remote](https://www.unfoldedcircle.com/), and see what each
box is playing.

The integration signs in to Fetch's own service with the same activation code
and PIN as the Fetch mobile app, and controls the boxes the way the app does.
It doesn't need Home Assistant or any other software, but it does need
internet access: FetchTV boxes aren't controlled over the local network.

## What you get

Every box on your Fetch account appears on the Remote as a media player, plus
a channel select (see below).

- **What's playing**: the programme title from the Fetch guide (for
  free-to-air *and* subscription channels), the channel name and logo, the
  episode title, and how far through the programme you are.
- **On / off** (standby), **play/pause, stop, fast forward, rewind**, mute.
- **Navigation**: arrows, OK, back, menu, guide, info, exit, the four colour buttons, text.
- **Channels**: channel up/down, and a channel list to pick from, with your
  visible free-to-air channels and every subscription channel on your plan.
- **Channel browser**: browse the Remote's media browser for **Free to air**
  and **Subscription** folders listing every channel with its logo; pick one to
  tune the box.
- **Channel select**: each box also has a second entity, "<box name> channel",
  listing the channels with the current one selected. Use it in dropdowns, UI
  pages and activities (select a channel, next/previous, first/last).
- **Record** button.

Not supported (yet): programme posters (Fetch's service only provides channel
logos), browsing recordings.

## What you need

- An Unfolded Circle Remote with internet access.
- Your Fetch **activation code** and **PIN** - the ones you use to sign in to
  the Fetch mobile app. The activation code is under My Account on
  [fetch.com.au](https://www.fetch.com.au/).

The activation code and PIN are stored on the Remote (or in the Docker
container's config folder) so the driver can sign in again after a restart.

## Install it on the Remote (easiest)

The driver runs on the Remote itself. No other hardware needed.

1. Download the latest `uc-intg-fetchtv-...-aarch64.tar.gz` file from the
   [Releases page](https://github.com/dgaust/uc-fetchtv/releases).
2. Open the Remote's web configurator in a browser. Go to
   **Integrations → Add new → Install custom** and upload the file.
3. Run the setup and type in your activation code and PIN.

Your boxes will appear, named the same as in the Fetch app.

### Updating

Do the same steps with the newer release file, but tick
**"Update existing driver"** when uploading. Your settings are kept, so you
won't need to set it up again. (This needs Remote firmware 2.9.3 or newer.)

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

### Using Cosmos?

In Cosmos, go to **ServApps → Start ServApp → Import Compose File**, paste the
contents of
[`cosmos-compose.json`](https://raw.githubusercontent.com/dgaust/uc-fetchtv/main/cosmos-compose.json)
and follow the installer.

### If the Remote can't find the driver automatically

When adding the integration on the Remote, open the advanced settings and
replace the server name with its IP address. Or tell the driver its own
address with `UC_INTEGRATION_INTERFACE: "192.168.1.50"` (this server's LAN IP)
in the compose file's `environment`, and recreate the container.

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
reconnecting), the mapping of box state to what the Remote shows, and the
driver end to end over the Remote's websocket API. CI runs them on every push.

### Releases (automated)

1. Change `"version"` in `intg-fetchtv/driver.json` (say `0.2.0`).
2. Commit and push, then tag it: `git tag v0.2.0 && git push origin v0.2.0`

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
  entities.py      turns each box into the media player the Remote shows
  config.py        saves the account and box list
  shared.py        shared setup used by the other files
tests/             standalone test scripts + a fake FetchTV service
Dockerfile         Docker image for running on a server/NAS
docker-compose.yml ready-to-use Docker setup
cosmos-compose.json one-click install for Cosmos servers
```

### How it works (the short version)

- The driver signs in over HTTPS, then keeps one websocket open to
  `messages.fetchtv.com.au`. Boxes report their state over it (channel,
  title, live/paused, standby) as it changes, and are asked again every
  minute. Remote keys and channel changes go out over the same socket.
- Channel names, logos and the programme guide come from Fetch's guide API.
  Subscription channels use their guide id as their channel id; free-to-air
  channel ids are regional variants of a guide id. Both are matched through
  the guide's channel list, so the programme shows on both.
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

[MPL-2.0](LICENSE)
