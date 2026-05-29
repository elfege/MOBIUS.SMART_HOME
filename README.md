# MOBIUS.SMART_HOME

A self-hosted smart-home automation platform that migrates Hubitat Groovy apps
to a Python web stack — moving the brains off the hub and onto a real database,
without giving up local-only operation.

> **Status:** active development. Single-operator deployment today (the author's
> home). Public source release under BSL-1.1 (see [License](#license)). The
> installation workflow is being reworked — see [Installation](#installation).

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
- **Matter support.** Embedded `python-matter-server`; matter nodes appear in
  the UI with online/last-seen staleness highlights and a quick-link back to
  the Hubitat editor for any device also paired through Hubitat.
- **Contract-drift watcher.** Polls Hubitat's platform version on a schedule
  and runs a canary against the admin API; surfaces deltas as soon as a hub
  firmware update changes a wire format.

## Currently shipped apps

| Type                       | Status   | Notes                                                         |
|----------------------------|----------|---------------------------------------------------------------|
| Advanced Motion Lighting   | Shipped  | Full-parity port of the Groovy app: motion-driven control, memoization of user overrides, mode-specific timeouts/dim levels, illuminance gating, pause/resume. |
| Fan Automation             | Shipped  | Temp/humidity-driven exhaust/ceiling-fan control.             |
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
   │ behind nginx HTTPS       │         │ (python-matter-server)│
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
| `matter-server`     | 5580                | Matter fabric, surfaced via the app        |

### Repository layout

```
.
├── app.py                            # FastAPI entry point
├── apps/                             # app types (advanced_motion_lighting,
│                                     #            fan_automation, samsung_tv)
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
