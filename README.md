# FetchTV for Unfolded Circle Remote

Control your [FetchTV](https://www.fetch.com.au/) boxes from an
[Unfolded Circle Remote](https://www.unfoldedcircle.com/): see what's playing,
pick channels from a browser with logos, and use every key on a ready-made
Remote 3 layout.

You sign in with the same activation code and PIN as the Fetch mobile app.

> **Needs the internet.** The driver never talks to your boxes directly. It
> controls them through Fetch's online service (`apis.fetchtv.com.au` and
> `messages.fetchtv.com.au`), the service the Fetch mobile app uses. Your
> Remote or server needs internet access, and it stops working if Fetch's
> service is down or your internet is out.

> Unofficial. Not made or endorsed by Fetch, and it may break if Fetch changes
> their service.

## Install

### On the Remote

1. Download the latest `uc-intg-fetchtv-...-aarch64.tar.gz` from the
   [Releases page](https://github.com/dgaust/uc-fetchtv/releases).
2. In the Remote's web configurator go to **Integrations → Add new → Install
   custom** and upload the file.
3. Run the setup and enter your activation code and PIN.
4. Choose the entities to add.

To update, upload the newer file with **Update existing driver** ticked
(Remote firmware 2.9.3 or newer). After an update that adds entities, add them
in the web configurator, and re-add a media player or remote to get new
features.

**Debug logging.** The Remote has no log level setting for a driver, so run the
FetchTV setup again and tick **Debug logging**, then download the log from
**Settings → Development: Logs** in the web configurator. Untick it afterwards.
See [Logs and debugging](docs/troubleshooting.md#logs-and-debugging).

### In Docker

```yaml
services:
  uc-fetchtv:
    image: ghcr.io/dgaust/uc-fetchtv:latest
    container_name: uc-fetchtv
    restart: unless-stopped
    network_mode: host   # required so the Remote can discover it
    environment:
      UC_INTEGRATION_HTTP_PORT: "10092"
      UC_LOG_LEVEL: "INFO"   # DEBUG when troubleshooting (see below)
    volumes:
      - ./config:/config
```

```bash
docker compose up -d
```

Then **Integrations → Add new** on the Remote and pick FetchTV.

**Debug logging.** Set `UC_LOG_LEVEL` to `DEBUG` in the compose file and recreate
the container with `docker compose up -d`. Then read the log:

```bash
docker logs -f uc-fetchtv
```

Add `--since 10m` to see only the last ten minutes. Set it back to `INFO` when
you're done: debug logs every message to and from the boxes. Your activation
code, PIN and anything you type into a box are never logged. See
[Logs and debugging](docs/troubleshooting.md#logs-and-debugging) for what to
look for.

### In Cosmos Cloud

1. In Cosmos, go to **ServApps** and choose **Import Compose File**. Paste the
   contents of [`cosmos-compose.json`](cosmos-compose.json), pick where to keep
   the driver's settings, and install. It runs with host networking, restarts
   automatically and updates itself when a new image is published.
2. The app has no web page, so Cosmos creates no URL for it. On the Remote go to
   **Integrations → Add new**, pick FetchTV and enter your activation code and PIN.
3. If the Remote can't find the driver, open the container's **Docker** tab, add
   an environment variable `UC_INTEGRATION_INTERFACE` set to the Cosmos server's
   LAN IP, and press **Update Container**.

To update by hand, open the container's **Overview** and press the update arrow
(it pulls the newest image and recreates the container).

**Debug logging.** Open the container's **Docker** tab, set the environment variable
`UC_LOG_LEVEL` to `DEBUG` (add it if it isn't listed), and press **Update
Container**. The output is on the container's **Logs** tab. Set it back to `INFO`
afterwards. Note that a container update or recreate clears the Logs tab, so
reproduce the problem after switching to `DEBUG`.

| Variable | Default | |
| --- | --- | --- |
| `UC_INTEGRATION_HTTP_PORT` | `10092` | Port the driver listens on |
| `UC_INTEGRATION_INTERFACE` | all | LAN address to announce, e.g. `192.168.1.50` (set it if the Remote can't find the driver) |
| `UC_CONFIG_HOME` | `/config` | Where settings are saved |
| `UC_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. `DEBUG` logs every message to and from the boxes |
| `UC_FETCH_LEGACY_HANDSHAKE` | off | Set to `1` to introduce the driver to Fetch the way versions before 0.4.1 did (old headers, shared device id, plain cookie), instead of like the Fetch app. For telling whether a problem comes from that |
| `UC_SESSION_GRACE_S` | `600` (`0` on the Remote) | Seconds the driver keeps its link to Fetch after the Remote sleeps or disconnects, so a Remote that wakes soon doesn't sign in again. `0` closes it at once |

## Entities and commands

Each box is added as a media player, a channel select, a remote and five sensors.

### Media player

| | |
| --- | --- |
| Power | on, off, toggle (pressed only when it changes something; while a box is unavailable, off and toggle are refused and on sends a wake) |
| Playback | play/pause, stop, fast forward, rewind, record |
| Navigation | up, down, left, right, OK, back, menu, guide, info, exit |
| Channels | channel up/down, number pad, channel list, channel browser (free to air and subscription, with logos and what's on now) |
| Recordings | the browser lists the box's recordings, newest first, with a folder for each series; pick one to play it on the box |
| Volume | set the box's volume, up, down, mute |
| Start over | the `START_OVER` command restarts the programme that is on now |
| Other | mute, text, red, green, yellow, blue |
| Shows | programme title, channel name and logo, episode, progress |

### Channel select

Pick a channel from a list: select, next, previous, first, last.

Playing a channel or recording on a box that is in standby wakes it first, then plays.

### Sensors

Five per box, for automations: **recordings** (how many are on the box), **scheduled recordings** (how many are scheduled), **recording now** (Yes or No), **free space** (GB) and **disk used** (%). They update when the box sends its full report: at start-up, a few seconds after a recording starts or ends, and when you open the Recordings folder.

### Remote

Every command below can be used in activities and macros. `DIGIT_0` to
`DIGIT_9` are the number keys.

```
CURSOR_UP  CURSOR_DOWN  CURSOR_LEFT  CURSOR_RIGHT  CURSOR_ENTER
BACK  EXIT  MENU  GUIDE  INFO  TEXT
PLAY_PAUSE  STOP  FAST_FORWARD  REWIND  RECORD  MUTE_TOGGLE
RED  GREEN  YELLOW  BLUE
CHANNEL_UP  CHANNEL_DOWN  DIGIT_0 ... DIGIT_9
START_OVER  VOLUME_UP  VOLUME_DOWN
```

To type text into the box (for example in a search box), send the command `TYPE:` followed by the text, such as `TYPE:dirty jobs`. Letters, digits and punctuation only, up to 64 characters. The text is typed in the background at about seven characters a second and the command returns at once, so in a macro leave a pause (about 150 ms per character) before the next command, such as OK.

Default Remote 3 layout (change it in the web configurator):

| Button | Does |
| --- | --- |
| D-pad, OK | cursor keys, select |
| Back | back |
| Channel up/down | channel up/down |
| Mute | mute |
| Menu | guide (hold: menu) |
| Record | record |
| Previous, Play, Stop, Next | rewind, play/pause, stop, fast forward |

Screens: **Numbers** (number pad and Text) and **Keys** (colours, Info, Exit, Start over).
Home and Power are left alone, and the volume commands are not placed on a button or screen: your Remote's volume buttons usually control the TV or amplifier.

## More

[Troubleshooting](docs/troubleshooting.md) ·
[Development](docs/development.md)

## Credits

Fetch doesn't document its service. This project builds on what
[jinxo13/pyfetchtv](https://github.com/jinxo13/pyfetchtv) worked out about it
(endpoints, messages, key names); no code is copied. It uses the
[Unfolded Circle Python integration library](https://github.com/unfoldedcircle/integration-python-library).

## License

[MPL-2.0](LICENSE). Third-party licences are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
