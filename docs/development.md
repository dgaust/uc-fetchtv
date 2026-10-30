# Development

## Run from source

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

## Tests

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

## Matching the Fetch app

So that Fetch's service sees an ordinary client, requests mirror what the Fetch
Android app (3.33.2, version code 6562) sends, found by reading the app itself:

- HTTP: `X-FTV-Capabilities: no_pin,android,v3.33.2.6562,tenplay_v2`,
  `X-FTV-Timeout: 30`, `X-FTV-DeviceID`, `User-Agent: okhttp/4.12.0`.
- Websocket: `wss://messages.fetchtv.com.au/v2/message/ws/messages?auth=<cookie>`
  with the capabilities and device id headers and no User-Agent. The app also
  sends a `verimatrix_id` (a DRM id); it isn't needed.
- The device id is a random 16-character hex string made once per installation
  and kept in `config.json` (the app sends its Android id, the same shape).

When the app is updated, bump `APP_VERSION` in `fetchtv.py` to its
`<version>.<version code>`.

## Releases (automated)

1. Change `"version"` in `intg-fetchtv/driver.json` (say `0.5.0`).
2. Commit and push, then tag it: `git tag v0.5.0 && git push origin v0.5.0`

CI then runs the tests, builds the install file for the Remote, and publishes
a **pre-release (beta)** on GitHub with the file and its checksum. Matching
Docker images are published at the same time. Once a beta has been tested on
a real Remote, untick "Set as a pre-release" on GitHub.

The install file can also be built locally with `bash build-intg.sh` (needs
Docker).

## What's in the repo

```
intg-fetchtv/
  driver.json      driver details + the setup screen (activation code, PIN)
  driver.py        starting point; handles connect/disconnect/standby
  setup_flow.py    first-time setup: signs in and finds the boxes
  box_manager.py   keeps the Fetch connection alive and updates the Remote
  fetchtv.py       talks to Fetch's service (sign-in, box messages, guide)
  entities.py      turns each box into a media player and a channel select, and builds the browser
  remote_entity.py the remote entity: every key, default buttons and pages
  sensors.py       status sensors: recordings, scheduled, recording now, disk
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

## How it works (the short version)

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
- Playing a recording is `PLAY_ASSET` with `assetType: "RECORDING"` and the
  recording's `id`. Send the id as a JSON **number**: a box that holds the
  recording itself (the Mighty) answers "not found locally" to a string, while
  a box that streams it from another (the Mini) accepts either. Some ids are
  larger than 2^53 and arrive rounded in the report; send them as they come,
  the box rounds the same way.
- After every channel change a box also sends `PLAYBACK_STOPPED` (the old
  stream stopping). The driver ignores it as a state and asks the box for its
  real state instead.
- The driver remembers your account and box list, so after a restart your
  boxes are back straight away with no re-setup.
- The protocol isn't documented by Fetch. This project builds on what
  [pyfetchtv](https://github.com/jinxo13/pyfetchtv) worked out.
- The driver id is `fetchtv` and stays the same across versions, so the
  Remote treats a new upload as an update.
