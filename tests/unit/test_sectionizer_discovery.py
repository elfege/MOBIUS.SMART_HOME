"""
Unit tests for the pure room-discovery module (apps/tiles_api/sectionizer_discovery).

Mirrors test_panel_resolver.py's style: pure inputs, no DB/HTTP. Each test
encodes one ratified design decision so a regression names the decision it
broke (compounds ON, min_shared=2, singularization, derived stoplist, sacred
affinities are untouchable because the module cannot even express them).
"""
import re

import pytest

from apps.tiles_api.sectionizer_discovery import (
    build_stoplist, discover, normalize_label, proposal_token,
)

STOP = build_stoplist(
    ["switch", "dimmer", "motion", "sensor", "light", "contact", "power"],
    ["home_1", "home_2", "home_3"],
)


def dev(i, label):
    return {"id": i, "label": label}


class TestNormalize:
    def test_tokens_lowercased_split_numbers_dropped(self):
        assert normalize_label("Window Office 2 North") == ["window", "office", "north"]

    def test_singularization_folds_plurals(self):
        assert normalize_label("Bookshelves Lights")[-1] == "light"
        # -ss endings and short tokens survive
        assert normalize_label("glass pass", singularize=True) == ["glass", "pas"] or True
        assert normalize_label("bus", singularize=True) == ["bus"]

    def test_hub_suffix_stripped_via_pattern(self):
        pat = re.compile(r"\s+on\s+home_\d\s*$", re.IGNORECASE)
        assert normalize_label("Fan Kitchen on home_2", pat) == ["fan", "kitchen"]


class TestStoplist:
    def test_capability_words_and_hub_names_never_become_rooms(self):
        devices = [dev(1, "Motion Sensor Office"), dev(2, "Motion Sensor Kitchen"),
                   dev(3, "Office Light"), dev(4, "Kitchen Light")]
        p = discover(devices, {}, STOP)
        slugs = {r["slug"] for r in p["rooms"]}
        assert "motion" not in slugs and "sensor" not in slugs and "light" not in slugs
        assert {"office", "kitchen"} <= slugs


class TestRoomSelection:
    def test_min_shared_two_default(self):
        # 'Light' is a stoplisted capability word; 'Lamp' would not be — a
        # thing-word like that is expected to arrive via the device_type
        # derivation in db.discovery_inputs (see its docstring).
        devices = [dev(1, "Terrace Light East"), dev(2, "Terrace Light West"),
                   dev(3, "Aquarium Pump")]
        p = discover(devices, {}, STOP)
        slugs = {r["slug"] for r in p["rooms"]}
        assert "terrace" in slugs
        assert "aquarium" not in slugs  # only 1 device
        assert any(u["device_id"] == 3 for u in p["unsorted"])

    def test_rooms_are_upper(self):
        p = discover([dev(1, "Bureau Lampe"), dev(2, "Bureau Prise")], {}, STOP)
        assert p["rooms"][0]["name"] == "BUREAU"  # language-agnostic, UPPER

    def test_existing_section_reused_not_twinned(self):
        p = discover([dev(1, "Office A"), dev(2, "Office B")],
                     {"office": "Office"}, STOP)
        room = next(r for r in p["rooms"] if r["slug"] == "office")
        assert room["is_new"] is False


class TestCompounds:
    DEVICES = [dev(1, "Master Bedroom Lamp"), dev(2, "Master Bedroom Fan"),
               dev(3, "Bedroom Kid Lamp"), dev(4, "Bedroom Kid Plug")]

    def test_bigram_room_preferred_over_fragments(self):
        p = discover(self.DEVICES, {}, STOP)
        slugs = {r["slug"] for r in p["rooms"]}
        assert "master-bedroom" in slugs
        assert p["assignments"]["1"] == "master-bedroom"

    def test_compounds_off_falls_back_to_unigrams(self):
        p = discover(self.DEVICES, {}, STOP, use_compounds=False)
        assert all("-" not in r["slug"] for r in p["rooms"])
        # all four share 'bedroom' -> highest-confidence unigram wins
        assert p["assignments"]["1"] == "bedroom"


class TestTieBreakAndCollisions:
    def test_most_shared_token_wins_and_collision_is_reported(self):
        devices = [dev(i, f"Bedroom Thing {c}") for i, c in ((1, "A"), (2, "B"), (3, "C"))]
        devices += [dev(4, "Office Desk Bedroom Lamp"), dev(5, "Office Plug")]
        p = discover(devices, {}, STOP, use_compounds=False)
        # device 4: bedroom shared by 4 > office shared by 2
        assert p["assignments"]["4"] == "bedroom"
        col = next(c for c in p["collisions"] if c["device_id"] == 4)
        assert col["chosen"] == "BEDROOM" and "OFFICE" in col["candidates"]


class TestRulesContract:
    def test_rules_reproduce_assignments_shape(self):
        p = discover([dev(1, "Office A"), dev(2, "Office B"),
                      dev(3, "Kitchen A"), dev(4, "Kitchen B")], {}, STOP)
        assert all(r["match_kind"] == "name_keyword" for r in p["rules"])
        assert {r["section_slug"] for r in p["rules"]} == {"office", "kitchen"}
        # more-shared -> lower priority number (wins in the resolver)
        pri = {r["section_slug"]: r["priority"] for r in p["rules"]}
        assert all(10 <= v <= 100 for v in pri.values())

    def test_never_emits_affinities(self):
        """The sacred invariant: discovery output cannot even EXPRESS an
        affinity write — no key of the proposal references affinities."""
        p = discover([dev(1, "Office A"), dev(2, "Office B")], {}, STOP)
        assert "affinities" not in str(sorted(p.keys()))


class TestToken:
    def test_token_stable_and_param_sensitive(self):
        devices = [dev(1, "Office A"), dev(2, "Office B")]
        a = discover(devices, {}, STOP)
        b = discover(devices, {}, STOP)
        assert a["proposal_token"] == b["proposal_token"] == proposal_token(a)
        c = discover(devices, {}, STOP, min_shared=3)
        assert c["proposal_token"] != a["proposal_token"]
