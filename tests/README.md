# Test Suite — MOBIUS.SMART_HOME

Authored 2026-05-16 (overnight session). See
`docs/plans/test_suite_architecture_and_implementation_strategy_2026_05_16.md`
for the full design rationale.

## How to run

```bash
# Fast path — Tier 1 + Tier 2 (units + service mocks). ~30s total.
./venv_test/bin/pytest

# Specific tier
./venv_test/bin/pytest -m unit
./venv_test/bin/pytest -m service

# Single file
./venv_test/bin/pytest tests/unit/test_mesh_filter.py

# Single test by name pattern
./venv_test/bin/pytest -k mesh_filter

# Integration (requires running smarthome-postgres + smarthome-postgrest)
./venv_test/bin/pytest -m integration

# Everything
./venv_test/bin/pytest -m "unit or service or integration"
```

## Tiers

| Tier | Marker | What | Cost | Run when |
|------|--------|------|------|----------|
| 1 | `unit` | Pure functions, no I/O | ~1ms each | Every save |
| 2 | `service` | Real services with mocked HTTP/WS | ~50ms each | Every save |
| 3 | `integration` | Real postgres + postgrest | ~5s init / ~100ms each | Pre-commit |
| 4 | `e2e` | Live running stack | seconds | CI / pre-release |

## Conventions

- **One behavior per test function.** Long setup is fine; long assertion lists are not.
- **AAA pattern** — Arrange, Act, Assert — but don't add comment headers; let the blank lines do the work.
- **No network in unit + service tiers.** `responses` library raises on un-matched HTTP calls, so accidental real-HTTP shows up as a loud failure.
- **Factories over fixtures for data shapes.** See `tests/factories.py`. Fixtures are for *systems* (PostgREST mock, DeviceCache fake), not for data.
- **Async tests just work.** `asyncio_mode = auto` in `pytest.ini`. Write `async def test_…` and it runs in an event loop.

## Adding a test

1. Pick the right tier (mostly: pure logic → unit, anything touching a service collaborator → service).
2. Name the file `tests/<tier>/test_<subject>_<expected_outcome>.py`.
3. Use factories from `tests/factories.py` when you need a realistic data shape.
4. Use mocker (`mocker` fixture) for one-off patches; declare module-level fixtures in `tests/conftest.py` only when reused.

## Bug-discovery protocol

If a new test fails because of a *real* bug in the app:
- Mark the test as expected-to-pass (don't `xfail` it).
- Open the bug — but don't change app behavior to make the test pass without
  also reporting the fix in commits/handoff.
- Commit the test and the fix together: `fix(<area>): <bug>` with the test
  file mentioned in the message.

If a new test fails because the test is wrong:
- Fix the test. Re-run. No special commit treatment.

## What's NOT tested (deliberate)

- FastAPI route plumbing (thin wrappers around tested services).
- Jinja2/HTML rendering.
- PostgREST itself (it's a black-box dependency).
- Hubitat Maker API quirks (mocked at HTTP boundary).
- Matter protocol details (mocked at client boundary).
- Frontend JavaScript.

See section 7.6 of the plan doc for the full non-goals list.
