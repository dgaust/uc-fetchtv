# Notes: controlling the boxes without the internet

The driver controls boxes through Fetch's cloud (see the README). These notes
record what was found when looking for a way to talk to a box directly on the
local network. Nothing here is used by the driver yet.

Checked on two Fetch boxes (a Mighty and a Mini) on the same LAN as the driver,
28 October 2026, with plain connection attempts and `GET` requests only. No
commands were sent to the boxes.

## What a box exposes

| Port | What it is | Notes |
| --- | --- | --- |
| 49152/tcp | **UPnP / DLNA media server** ("Fetch DLNA1.5 DMS", Linux 4.1.20) | Standard. `GET /MediaServer.xml` returns the device description with a `ContentDirectory` service (`/web/cds_control`) and a `ConnectionManager`. This is where recordings are served from (pyfetchtv reads recordings' `dlna_url`). It lists and streams media; it is not a remote control. |
| 80/tcp | Small custom HTTP server | `GET /` answers `200 OK` with `Content-Length: 55` but closes the connection before any body. Other paths and `OPTIONS` get no reply; a malformed request gets `400 ... Cannot parse HTTP request`. Purpose unknown. |
| 8060/tcp, 56790/tcp | Small custom HTTP server (same family as port 80) | Every path tried (about 70 common API, status and remote-control names) returns `404 Not Found` in plain text. It is not the Roku ECP protocol, although 8060 is Roku's port. |
| 111/tcp | rpcbind | Standard. |

No other TCP ports were open (all 65,535 checked). The boxes did not answer
SSDP searches (multicast or sent straight at them), and the only mDNS services
on the network were other devices'. A box's LAN address does appear in the
state it reports to Fetch's cloud (`ipAddress`).

## What this means

- **Recordings**: DLNA on 49152 can list and stream a box's recordings, but it
  is a media *server* only (a `ContentDirectory` and a `ConnectionManager`, no
  `AVTransport` or `RenderingControl`), so it can't be told to play one on the
  box. The driver's recordings list comes from the cloud report instead, and
  playing a recording on the box still needs Fetch's own message (unknown).
- **Remote control, state and channel changes**: no documented local interface
  was found. If the Fetch mobile app talks to the box directly when you are on
  the same Wi-Fi, it would be through ports 80, 8060 or 56790, whose requests
  and any authentication are unknown.

## How to find out more

The reliable way is to watch what the Fetch mobile app does:

1. Capture the phone's traffic while pressing remote keys with the phone on the
   same Wi-Fi as the box (a router or access point with packet capture, a
   mirrored switch port, or a capture proxy on the phone), once with the phone
   on mobile data and once on Wi-Fi.
2. If traffic goes from the phone to ports 80, 8060 or 56790 on the box when on
   Wi-Fi, that is the local protocol: record the paths, headers and any token.
3. If it only ever goes to `messages.fetchtv.com.au`, control is cloud-only and
   the internet requirement can't be removed.

Anything found should go through the same checks as the cloud protocol: only
use it against your own boxes, and keep any token out of logs.
