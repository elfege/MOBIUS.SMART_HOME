# MOBIUS.SMART_HOME

A self-hosted, local-first home-automation platform: a real automation engine
(Python + Postgres) that controls devices over **whatever speaks to them** —
Matter (natively, as its own controller), Hubitat hubs (as radio gateways),
and plain IP (TVs, speakers). It began as a migration of Hubitat Groovy apps
to a Python web stack; it is **no longer Hubitat-bound, and not meant to stay
that way**.

> **Status:** active development, beta. Single-operator deployment today (the
> author's home). Public source release under BSL-1.1 (see
> [License](#license)). The installation workflow is being reworked — see
> [Installation](#installation).

## Why

Hubitat's strengths are its radios and its local-first ethos. Its weaknesses
are everything around them: the Maker API adds round-trip latency and overloads
the hub under fan-out, app state lives in Groovy globals, and a firmware
update can silently change a contract. This project moves the automation
engine — state, scheduling, multi-hub coordination, observability — to a stack
that can actually be queried, tested, versioned, and reasoned about.

The hub, meanwhile, is being demoted from platform to peripheral, in stages:

1. **Done — brains off the hub.** Automations, state, and scheduling run here;
   hubs execute commands (admin API primary, Maker API opt-in fallback).
2. **Done — Matter without the hub.** MOBIUS runs its own Matter controller
   and holds its own admin fabric on each device: direct protocol control,
   hub not in the loop.
3. **Done — IP devices without any hub.** Samsung TVs (multi-TV,
   WebSocket/WoL), Sonos speakers (local UPnP TTS, cloud-free); Hisense TV
   support in progress.
4. **Planned — native radios.** Dedicated Zigbee / Z-Wave / Thread interfaces
   (e.g. SLZB-06M, Zooz ZST39 LR, OpenThread border router) so the remaining
   hub-radio dependency becomes optional hardware, not an architecture.

## What it does today

- **Multi-instance apps.** Each automation is a row in `app_instances` with its
  own devices and settings. "Advanced Lights — Office" and "Advanced Lights —
  Bedroom" are two independent instances of the same app type; pause one, the
  other keeps running.
- **Matter, controlled directly.** MOBIUS runs its own Matter controller
  (`matterjs-server`, on the matter.js SDK) and holds an admin fabric on each
  device, so it drives Matter devices **directly over the Matter protocol** —
  Hubitat is only a fallback command path. Devices are discovered two ways,
  deduplicated by MAC → serial → name: from the selected Hubitat hub(s)
  (multi-hub, admin API) and directly over mDNS (`_matterc._udp`, no hub).
  Bulk commissioning is strictly sequential (one pairing window at a time) with
  a live CHIP-level log stream. Matter-over-Thread devices route through a hub
  with a built-in Thread border router (Hubitat C-8 / C-8 Pro) until native
  Thread lands.
- **Multi-hub.** Devices are classified to the hub that owns them (LAN/mesh-
  aware), with one hub flagged primary. Same-label duplicates across hubs are
  resolved by primary-hub precedence + Hubitat's `linkedDevice` mirror flag.
- **Admin API as the primary transport.** Outbound commands and device polls
  go through Hubitat's web-UI admin endpoints, not the Maker API. The Maker
  API is supported as an opt-in fallback, not a dependency. This avoids the
  Maker round-trip penalty entirely.
- **WebSocket eventsocket.** Live device events stream in over Hubitat's
  eventsocket; the dispatcher fans them out to subscribing instances. A
  reconcile poller + optional Maker webhook intake cover gaps.
- **Postgres as source of truth.** Schema is split across three Postgres
  schemas — `dshub` (substrate / hub roster / device cache), `dsapp`
  (automation: instances, subscriptions, memoization), `dscore` (system
  settings, health, audit) — exposed through a single `api` view schema for
  PostgREST, and built from **versioned migrations** (buildable from scratch).
- **IP-device drivers, no hub involved.** Samsung TVs: multi-TV, DB-backed
  per-instance controllers (Wake-on-LAN, WebSocket remote, per-model
  power-state handling back to 2014 H-series quirks). Sonos: local UPnP
  announcements/TTS, no cloud. Hisense (ADB-based) in progress.
- **Tile dashboard (absorbed from MOBIUS.TILES).** The former standalone
  real-time tile dashboard is being folded in as a React Native frontend
  (`frontend/tiles`) backed by `apps/tiles_api` — one platform, one database,
  no second stack. The old `MOBIUS.TILES` repos are no longer maintained.
- **Contract-drift watcher.** Polls Hubitat's platform version on a schedule
  and runs a canary against the admin API; surfaces deltas as soon as a hub
  firmware update changes a wire format.
- **Device name normalizer.** Optional, default-off maintenance pass that strips
  a trailing " on &lt;hub name&gt;" suffix from Hub Mesh linked-device labels
  directly on the hubs. Data-driven from the hubs' own location names, with a
  mandatory dry-run preview before anything is renamed.

## Currently shipped apps

| App                      | Status      | Notes |
|--------------------------|-------------|-------|
| Advanced Motion Lighting | Shipped     | Python port of the Hubitat Groovy app [Advanced Motion Lighting Management V2][aml-groovy] (same author): motion-driven control, memoization of user overrides, mode-specific timeouts/dim levels, illuminance gating, pause/resume. |
| Fan Automation           | Shipped (v2)| Light-driven fan control with a humidity anti-noise state machine (high → quiet → ramp) for extraction without the roar. |
| Screen Time Planner      | Shipped     | TV allowed only inside daily time windows (per-day, cross-midnight aware). Turning it on outside a window is cut in real time; optional delayed cut of a secondary power switch, plus wake-on-power suppression for TVs that boot on mains restore. |
| Power Management         | Shipped     | Average-watts threshold cutoffs for breaker-overload protection (pool pumps, EV chargers, dryers); trip state survives restarts. |
| Rules                    | Shipped     | Declarative case-based button/event automations (schema + interpreter) — replaced the author's Hubitat Mode Manager / Rule Machine instances. |
| Sonos Alarm              | Shipped     | Scheduled TTS/mp3 announcements on Sonos speakers over local UPnP — no cloud. |
| Humidifier               | Shipped     | Maintain a room's humidity: plug ON when the air is dry and the room occupied, OFF at target / room empty / window open. Port of the author's decade-stable Groovy app. |
| Samsung TV (driver)      | Shipped     | Multi-TV: DB-backed per-instance full remote controllers; WoL power-on; per-generation protocol fallbacks. |
| Hisense TV (driver)      | In progress | ADB-based control, mirrors the Samsung driver pattern. |

Hubitat Safety Monitor (HSM) and a handful of hub-native pieces intentionally
stay on the hub.

[aml-groovy]: <!-- TODO: replace with the public Hubitat Groovy repo URL for Advanced Motion Lighting Management V2 -->

## Architecture (one screenful)

```
                          ┌──────────────────────────┐
   Hubitat hub(s) ◄──────►│  admin API client        │
        │   eventsocket   │  (primary transport)     │
        ▼                 ├──────────────────────────┤
   ┌──────────┐  fan-out  │  webhook dispatcher      │◄── Maker API webhooks
   │ MOBIUS   │◄──────────┤  (port 5050)             │     (fallback intake)
   │ smart-   │           └──────────────────────────┘
   │ home     │    ┌──────────────────────────────────┐
   │ FastAPI  │◄──►│ Postgres  (dshub / dsapp /        │
   │ uvicorn  │    │            dscore  →  api views)  │
   └────┬─────┘    └──────────────────────────────────┘
        │                ▲
        │                │  PostgREST  (auto-generated REST from views)
        │                │
        ▼                ▼
   ┌──────────────────────────┐         ┌───────────────────────┐
   │ Jinja2 + ES6 UI          │         │ matter-server         │
   │ (+ React Native frontend │         │ (matterjs, matter.js) │
   │  in progress)            │         │ MOBIUS's admin fabric │
   │ behind nginx             │         └───────────────────────┘
   └──────────────────────────┘
```

### Services (Docker Compose)

| Service             | Default port (host) | Role                                       |
|---------------------|---------------------|--------------------------------------------|
| `smart-home`        | 5001 → 5000         | FastAPI app (uvicorn)                      |
| `nginx`             | 8082 / 8445         | HTTP / HTTPS reverse proxy + static        |
| `postgres`          | 5433 → 5432         | Database (substrate of record)             |
| `postgrest`         | 3002 → 3001         | Auto-REST over the `api` view schema       |
| `webhook-dispatcher`| 5050                | Single Hubitat target; fans out to clients |
| `matter-server`     | 5580                | Matter controller (`matterjs`); MOBIUS's admin fabric — direct Matter control |

### Repository layout

```
.
├── app.py                            # FastAPI entry point
├── apps/                             # app types (advanced_motion_lighting,
│                                     #   fan_automation, screen_time_planner,
│                                     #   power_management, rules, sonos,
│                                     #   humidifier, samsung_tv, hisense_tv,
│                                     #   tiles_api)
├── frontend/                         # React Native (Expo): tiles dashboard +
│                                     #   admin app (in progress)
├── services/                         # transport + infra
│   ├── hubitat_admin_client.py       #   primary transport (web-UI admin API)
│   ├── hubitat_eventsocket_client.py #   live event ingress
│   ├── hubitat_client.py             #   Maker API (opt-in fallback)
│   ├── device_to_hubs_classifier.py  #   multi-hub roster + dedup
│   ├── hub_contract_watch.py         #   firmware/contract drift watcher
│   ├── matter_client.py / matter_discovery.py
│   ├── device_cache* / instance_manager / reconcile_poll / mode_poller / ...
├── models/                           # Pydantic
├── psql/                             # versioned migrations (dshub/dsapp/dscore + api views)
├── templates/                        # Jinja2
├── static/                           # ES6 modules + jQuery + Chart.js
├── nginx/                            # reverse-proxy config (certs auto-generated)
└── docker-compose.yml
```

## Installation

> ⚠ The installation workflow is being reworked. The notes below describe the
> current developer-machine flow; a portable installer (project-local
> `start_utils.sh`, cross-platform setup) is in progress. Expect this section
> to change.

In short, today's flow is `./start.sh` (or `./deploy.sh` for a rebuild) on a
Docker host that can reach your devices on the LAN. The scripts source AWS
Secrets Manager for credentials — that dependency is what's being decoupled. A
full how-to lands when the new flow is in place.

## License

[Business Source License 1.1](LICENSE). Non-production use is permitted today;
the license converts to **Apache 2.0** on the Change Date (April 9, 2036). See
the `LICENSE` file for the exact terms, the Change Date, and the Change
License.

**TL;DR for the impatient:**

- Run it at home, on your own hub, for your own automations — fine.
- Read it, modify it, fork it, contribute back — fine.
- Sell it as a hosted service or bundle it into a commercial smart-home product —
  not without a commercial license until the Change Date.

## Contributing & issues

Bug reports, reproductions, and pull requests are welcome on the public
mirror. The hub-firmware surface is a moving target — if a Hubitat update
breaks something, an issue with the platform version and the failing wire
payload is the most useful thing you can send.

## A note on the repo layout

The canonical history lives in the private development repo
(`MOBIUS.SMART_HOME-dev`). A filtered, history-rewritten subset is published
to the public mirror (`MOBIUS.SMART_HOME`) — operator markdown (handoffs,
session histories, internal planning, personal notes) is stripped on the way
out so contributors see code, not paperwork.
