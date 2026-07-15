"""
Automatic room discovery — device labels -> a proposed section/rule layer.

PURE module (mirrors resolver.py): operates on already-fetched rows, no DB, no
HTTP. The route + db.py do the reading/writing. Design:
docs/plans/automatic_room_discovery_sectionizer_...md (assistant-3, ratified by
Architect 2026-07-14 with all six §7 recommendations adopted: compounds ON,
UPPER names, min_shared=2, singularization ON, seeded rules kept as fallback;
the dedicated-UNSORTED presentation is deferred to the review-sheet UI phase).

Invariants (the fix for the old TILES sectionizer's sins):
- Emits a PROPOSAL — nothing here writes anything.
- The proposal's rule rows are all `origin='auto'`; apply (db.py) replaces
  ONLY that layer. `panel_device_affinities` is NEVER produced here — operator
  placements are sacred by construction.
- The stoplist is DERIVED (capability/tile-type words from panel_tile_types +
  a tiny documented filler list), never a curated opinion blocklist.

Caveat (documented, accepted): the preview's `assignments` are computed here
with an earliest-in-label tie-break; at read time the resolver re-derives
placement from the emitted rules (priority ASC, pattern length DESC). When two
candidate rooms have EQUAL share-counts the resolver's tie-break can differ
from the preview's. Such devices are listed in `collisions` so the review
sheet shows exactly where judgment was applied.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Pattern, Sequence, Set, Tuple

# Words that name THINGS rather than PLACES. Capability/tile-type words arrive
# from the DB (panel_tile_types); this is only the small universal filler.
UNIVERSAL_FILLER = {
    "the", "a", "an", "my", "and", "of", "on", "in", "at", "for",
    "room", "area", "zone", "hub", "hubitat", "device",
    "multi",  # "(multi) sensor" wart in several live labels
}

_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)


def build_stoplist(tile_type_words: Iterable[str],
                   hub_names: Iterable[str]) -> Set[str]:
    """Derive the stoplist: capability/tile-type nouns (from the DB, so it
    auto-tracks new capabilities) + universal filler + hub name tokens (a hub
    name must never become a room)."""
    stop = set(UNIVERSAL_FILLER)
    for w in tile_type_words:
        for tok in _SPLIT_RE.split(str(w).lower()):
            if tok:
                stop.add(tok)
    for hub in hub_names:
        for tok in _SPLIT_RE.split(str(hub).lower()):
            if tok:
                stop.add(tok)
    return stop


def _singularize(token: str) -> str:
    """Light plural fold: trailing -s on tokens >3 chars (lights->light).
    Deliberately dumb — 'gps'/'sonos'-style words are protected by length or
    by being stoplisted; a rare wrong fold merely merges two candidate rooms."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalize_label(label: str,
                    suffix_pattern: Optional[Pattern] = None,
                    singularize: bool = True) -> List[str]:
    """Label -> ordered surviving tokens (pre-stoplist). Steps per design §3.1:
    strip the ' on <hub>' suffix, casefold + unicode-fold, split on
    punctuation, drop numbers/single chars, optionally fold plurals."""
    if not label:
        return []
    text = label
    if suffix_pattern is not None:
        cleaned = suffix_pattern.sub("", text).rstrip()
        if cleaned:
            text = cleaned
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    out: List[str] = []
    for tok in _SPLIT_RE.split(text.lower()):
        if not tok or len(tok) < 2 or tok.isdigit():
            continue
        out.append(_singularize(tok) if singularize else tok)
    return out


def _room_name(key: str) -> str:
    return key.upper()


def _room_slug(key: str) -> str:
    return key.replace(" ", "-")


def discover(devices: Sequence[Dict[str, Any]],
             existing_sections: Dict[str, str],
             stoplist: Set[str],
             suffix_pattern: Optional[Pattern] = None,
             min_shared: int = 2,
             use_compounds: bool = True,
             singularize: bool = True) -> Dict[str, Any]:
    """
    The whole algorithm (design §3): normalize -> frequency map (unigrams +
    adjacent bigrams) -> rooms (shared by >= min_shared devices) -> per-device
    assignment (compound first, then most-shared, then earliest-in-label) ->
    proposal with rule rows whose priority encodes confidence.

    Args:
        devices: [{'id': int, 'label': str}, ...] — the live roster (winners
                 only; caller filters is_present / is_name_duplicate).
        existing_sections: {slug: display_name} for the target profile —
                 reused (never twinned) when a discovered room collides.
        stoplist: from build_stoplist().
        suffix_pattern: device_name_normalizer's compiled ' on <hub>' stripper.
        min_shared: threshold N (ratified default 2).
        use_compounds: bigram rooms like MASTER BEDROOM (ratified ON).
        singularize: plural folding (ratified ON).

    Returns the Proposal dict (design §5) plus 'rules' rows ready for
    db.apply_auto_layer(), and a deterministic 'proposal_token' so apply can
    prove it commits exactly what was previewed.
    """
    # ---- tokenize (keep order for tie-break + bigram adjacency) -----------
    dev_tokens: Dict[int, List[str]] = {}
    labels: Dict[int, str] = {}
    for d in devices:
        did = int(d["id"])
        labels[did] = d.get("label") or d.get("name") or f"device {did}"
        toks = [t for t in normalize_label(labels[did], suffix_pattern, singularize)]
        dev_tokens[did] = toks

    surviving: Dict[int, List[str]] = {
        did: [t for t in toks if t not in stoplist] for did, toks in dev_tokens.items()
    }

    # ---- frequency maps (a token counts once per device) -------------------
    uni: Dict[str, Set[int]] = {}
    for did, toks in surviving.items():
        for t in set(toks):
            uni.setdefault(t, set()).add(did)

    bi: Dict[str, Set[int]] = {}
    if use_compounds:
        for did, toks in surviving.items():
            seen: Set[str] = set()
            for a, b in zip(toks, toks[1:]):
                seen.add(f"{a} {b}")
            for pair in seen:
                bi.setdefault(pair, set()).add(did)

    uni_rooms = {t: ids for t, ids in uni.items() if len(ids) >= min_shared}
    bi_rooms = {p: ids for p, ids in bi.items() if len(ids) >= min_shared}

    # ---- per-device assignment (design §3.5) --------------------------------
    assignments: Dict[int, str] = {}      # device_id -> room key
    collisions: List[Dict[str, Any]] = []
    unsorted: List[Dict[str, Any]] = []

    for did, toks in surviving.items():
        cand_bi = [p for p in bi_rooms if p in {f"{a} {b}" for a, b in zip(toks, toks[1:])}]
        cand_uni = [t for t in dict.fromkeys(toks) if t in uni_rooms]

        chosen: Optional[str] = None
        why = ""
        if cand_bi:
            # Longest shared compound wins; among equals, the most-shared.
            chosen = sorted(cand_bi, key=lambda p: (-len(bi_rooms[p]), toks.index(p.split()[0])))[0]
            why = f"compound '{_room_name(chosen)}' shared by {len(bi_rooms[chosen])}"
        elif cand_uni:
            best = sorted(cand_uni, key=lambda t: (-len(uni_rooms[t]), toks.index(t)))[0]
            chosen = best
            why = f"'{_room_name(best)}' shared by {len(uni_rooms[best])}"

        if chosen is None:
            unsorted.append({"device_id": did, "label": labels[did],
                             "why": "no shared keyword"})
            continue

        assignments[did] = chosen
        # A collision = at least one rejected alternative room. The chosen
        # compound's own unigrams don't count as alternatives (MASTER BEDROOM
        # vs MASTER is the same decision, not a competing one).
        alternatives = [c for c in dict.fromkeys(cand_bi + cand_uni)
                        if c != chosen and c not in chosen.split()]
        if alternatives:
            collisions.append({
                "device_id": did, "label": labels[did],
                "candidates": [_room_name(c) for c in [chosen] + alternatives],
                "chosen": _room_name(chosen), "why": why,
            })

    # ---- rooms actually used -> sections + rules ---------------------------
    used: Dict[str, Set[int]] = {}
    for did, room in assignments.items():
        used.setdefault(room, set()).add(did)

    rooms: List[Dict[str, Any]] = []
    rules: List[Dict[str, Any]] = []
    for key, ids in sorted(used.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        slug = _room_slug(key)
        shared = len(bi_rooms.get(key, uni_rooms.get(key, ids)))
        rooms.append({
            "slug": slug,
            "name": _room_name(key),
            "device_count": len(ids),
            "is_new": slug not in existing_sections,
            "why": f"{shared} devices share {_room_name(key)}",
        })
        # Confidence -> priority: more-shared wins (resolver sorts priority ASC
        # then pattern-length DESC, so compounds also naturally beat their own
        # unigrams at equal priority).
        rules.append({
            "section_slug": slug,
            "match_kind": "name_keyword",
            "pattern": key,
            "priority": max(10, 100 - shared),
        })

    proposal = {
        "rooms": rooms,
        "assignments": {str(d): _room_slug(r) for d, r in sorted(assignments.items())},
        "unsorted": sorted(unsorted, key=lambda u: u["label"]),
        "collisions": sorted(collisions, key=lambda c: c["label"]),
        "rules": rules,
        "params": {"min_shared": min_shared, "use_compounds": use_compounds,
                   "singularize": singularize},
    }
    proposal["proposal_token"] = proposal_token(proposal)
    return proposal


def proposal_token(proposal: Dict[str, Any]) -> str:
    """Deterministic digest of what apply would commit (rooms + rules +
    params). Apply recomputes and must match, proving the roster didn't move
    between preview and commit."""
    core = {k: proposal[k] for k in ("rooms", "rules", "params")}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
