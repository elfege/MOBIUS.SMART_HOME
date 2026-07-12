# MOBIUS.SMART_HOME

A self-hosted smart-home automation platform that migrates Hubitat Groovy apps
to a Python web stack — moving the brains off the hub and onto a real database,
without giving up local-only operation.

> **Status:** active development. Single-operator deployment today (the author's
> home). Public source release under BSL-1.1 (see [License](#license)). The
> installation workflow is being reworked — see [Installation](#installation).

## Currently shipped apps

| App                        | Released in | App version | Status   | Notes |
|----------------------------|-------------|-------------|----------|-------|
| Advanced Motion Lighting   | v3.3.11     | 2.0.0       | Shipped  | Python port of the Hubitat Groovy app [Advanced Motion Lighting Management V2][aml-groovy] (same author): motion-driven control, memoization of user overrides, mode-specific timeouts/dim levels, illuminance gating, pause/resume. |
| Fan Automation             | v3.3.11     | 1.0.0       | Shipped  | Temp/humidity-driven exhaust/ceiling-fan control with manual fan-level override switches and a post-override humidity-suppress window. |
| Screen Time Planner        | v4.8.0      | 2.0.0       | Shipped  | TV allowed only inside daily time windows (per-day, cross-midnight aware). Turning it on outside a window is cut in real time; optional delayed cut of a secondary power switch, plus wake-on-power suppression for TVs that boot on mains restore. |
| Samsung FastAPI router     | v3.3.11     | —           | Shipped (single-TV) | Standalone FastAPI router + Jinja2 page mounted under `/samsung-tv`: Wake-on-LAN, WebSocket remote, token-paired SmartView. **Currently single-tenant** — one process-wide client, supports exactly one TV. IP/MAC/token come from env vars (`SAMSUNG_TV_IP` / `SAMSUNG_TV_MAC` / `SAMSUNG_TV_TOKEN`) and can be retargeted live via `POST /api/samsung-tv/configure`. Multi-TV support is a planned refactor (either promote to a real app type or registry-keyed routing). |

Hubitat Safety Monitor (HSM) and a handful of hub-native pieces intentionally
stay on the hub.

The **Released in** column is the platform tag in which the app first appeared
(see [Releases](https://github.com/elfege/MOBIUS.SMART_HOME/releases)). The
**App version** column is the app's own internal `VERSION` constant — bumped
independently of platform versions when the app itself changes.

[aml-groovy]: <!-- TODO: replace with the public Hubitat Groovy repo URL for Advanced Motion Lighting Management V2 -->

## Why

Hubitat's strengths are its radios and its local-first ethos. Its weaknesses
are everything around them: the Maker API adds round-trip latency and overloads
the hub under fan-out, app state lives in Groovy globals, and a firmware
update can silently change a contract. This project keeps the hub as a radio
gateway and moves the automation engine — state, scheduling, multi-hub
coordination, observability — to a stack that can actually be queried, tested,
versioned, and reasoned about.

## What it does today

- **Multi-instance apps.** Each automation is a row in `app_instances` with its
  own devices and settings. "Advanced Lights — Office" and "Advanced Lights —
  Bedroom" are two independent instances of the same app type; pause one, the
  other keeps running.
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
  PostgREST.
- **Matter, controlled directly.** MOBIUS runs its own Matter controller
  (`matterjs-server`, on the matter.js SDK) and holds an admin fabric on each
  device, so it drives Matter devices **directly over the Matter protocol** —
  Hubitat is only a fallback command path. Devices are discovered two ways,
  deduplicated by MAC → serial → name: from the selected Hubitat hub(s)
  (multi-hub, admin API) and directly over mDNS (`_matterc._udp`, no hub).
  Bulk commissioning is strictly sequential (one pairing window at a time) with
  a live CHIP-level log stream. Matter-over-Thread devices route through a hub
  with a built-in Thread border router (Hubitat C-8 / C-8 Pro).
- **Contract-drift watcher.** Polls Hubitat's platform version on a schedule
  and runs a canary against the admin API; surfaces deltas as soon as a hub
  firmware update changes a wire format.
- **Device name normalizer.** Optional, default-off maintenance pass that strips
  a trailing " on &lt;hub name&gt;" suffix from Hub Mesh linked-device labels
  directly on the hubs. Data-driven from the hubs' own location names, with a
  mandatory dry-run preview before anything is renamed.

## Currently shipped apps

| Type                       | Status   | Notes                                                         |
|----------------------------|----------|---------------------------------------------------------------|
| Advanced Motion Lighting   | Shipped  | Full-parity port of the Groovy app: motion-driven control, memoization of user overrides, mode-specific timeouts/dim levels, illuminance gating, pause/resume. |
| Fan Automation             | Shipped  | Temp/humidity-driven exhaust/ceiling-fan control.             |
| Screen Time Planner        | Shipped  | TV allowed only inside daily time windows (per-day, cross-midnight aware). Turning it on outside a window is cut in real time; optional delayed cut of a secondary power switch, plus wake-on-power suppression for TVs that boot on mains restore. |
| Samsung TV (driver)        | Shipped  | Standalone driver/controller, not a multi-instance app type.  |

Hubitat Safety Monitor (HSM) and a handful of hub-native pieces intentionally
stay on the hub.

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
   │ Jinja2 + ES6 modules UI  │         │ matter-server         │
   │ behind nginx HTTPS       │         │ (matterjs, matter.js) │
   └──────────────────────────┘         └───────────────────────┘
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
│                                     #            fan_automation, screen_time_planner,
│                                     #            samsung_tv)
├── services/                         # transport + infra
│   ├── hubitat_admin_client.py       #   primary transport (web-UI admin API)
│   ├── hubitat_eventsocket_client.py #   live event ingress
│   ├── hubitat_client.py             #   Maker API (opt-in fallback)
│   ├── device_to_hubs_classifier.py  #   multi-hub roster + dedup
│   ├── hub_contract_watch.py         #   firmware/contract drift watcher
│   ├── matter_client.py / matter_discovery.py
│   ├── device_cache* / instance_manager / reconcile_poll / mode_poller / ...
├── models/                           # Pydantic
├── psql/                             # init + migrations (dshub/dsapp/dscore + api views)
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
Docker host that can reach Hubitat on the LAN. The scripts source AWS Secrets
Manager for credentials — that dependency is what's being decoupled. A full
how-to lands when the new flow is in place.

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
