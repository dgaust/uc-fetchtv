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
    volumes:
      - ./config:/config
```

```bash
docker compose up -d
```

Then **Integrations → Add new** on the Remote and pick FetchTV. For Cosmos,
import [`cosmos-compose.json`](cosmos-compose.json).

| Variable | Default | |
| --- | --- | --- |
| `UC_INTEGRATION_HTTP_PORT` | `10092` | Port the driver listens on |
| `UC_INTEGRATION_INTERFACE` | all | LAN address to announce, e.g. `192.168.1.50` (set it if the Remote can't find the driver) |
| `UC_CONFIG_HOME` | `/config` | Where settings are saved |
| `UC_LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

## Entities and commands

Each box is added as a media player, a channel select, a remote and three sensors.

### Media player

| | |
| --- | --- |
| Power | on, off, toggle |
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

Three per box, for automations: **recordings** (how many are on the box), **scheduled recordings** (how many are scheduled) and **recording now** (Yes or No). They update when the box sends its full report, at start-up and when you open the Recordings folder.

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

To type text into the box (for example in a search box), send the command `TYPE:` followed by the text, such as `TYPE:dirty jobs`. Letters, digits and punctuation only, up to 64 characters.

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
