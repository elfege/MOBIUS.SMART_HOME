"""
Tier-1 observational smoke: the RN admin app (the cutover front door at /) boots
in a real browser and renders its shell. Read-only — no actuation.

This is the seed the E2E plan's per-app journeys grow from (weeklyWindows editor,
pause/resume-reconcile, Matter pairing, etc.); it proves the harness + the bundle
load end-to-end before those richer journeys are worth writing.
"""


def test_rn_admin_bundle_boots(page):
    """`/` serves the RN admin bundle and it mounts (root has rendered content)."""
    page.goto("/", wait_until="networkidle")
    # react-native-web renders into #root; a mounted app leaves it non-empty.
    root = page.locator("#root")
    root.wait_for(state="attached", timeout=15_000)
    assert root.inner_text(timeout=15_000).strip() != "", \
        "RN admin root mounted but rendered empty — bundle failed to boot"


def test_home_shows_automations_tile(page):
    """The home shell presents the Automations native surface (the one ported
    tile today). Text assertion, not a screenshot — resilient + CVD-neutral."""
    page.goto("/", wait_until="networkidle")
    page.get_by_text("Automations", exact=False).first.wait_for(timeout=15_000)


def test_home_lists_a_legacy_migration_tile(page):
    """At least one '/legacy' surface is offered (the migration burndown is
    visible). Matter is the operator's active surface — assert it is reachable."""
    page.goto("/", wait_until="networkidle")
    page.get_by_text("Matter", exact=False).first.wait_for(timeout=15_000)
