"""
Matter command feedback — the USER-INPUT-BASED learning log.

Operator directive 2026-07-11: "implement new learning capability (separate
table) so we have a log of when it worked, didn't work — totally user-input
based" + "modal requires visual confirmation."

Flow (see static/js/controllers/matter-controller.js::sendMatterTest):
    1. UI sends a Matter test command (POST /api/matter/nodes/{id}/command).
    2. When it settles, the UI POSTs an ATTEMPT row here carrying what the
       API *claimed* (api_success / api_detail).
    3. The result modal then REQUIRES visual confirmation: the operator's
       "It worked" / "It didn't" click PATCHes the verdict onto the same row.
       Dismissing the modal leaves the row 'unverified' — still a data point.

The learning signal is the DIVERGENCE between api_success and
operator_verdict (matter-server acked but nothing actuated = the classic
failure this log exists to quantify). The `controller` column tags which
Matter controller generation produced the attempt so worked-rates can be
compared across the python-matter-server -> matterjs-server migration.

Storage: dshub.matter_command_feedback (api view for PostgREST) — created by
psql/migrate_matter_command_feedback_learning_log.sql. Standalone, no FKs,
no CASCADE (policy P5, audit MSG-609); rows denormalize node_id/label so
they survive device removal.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/matter/feedback", tags=["matter-feedback"])

# Which Matter controller generation is live. Env-overridable so the matterjs
# migration flips one compose var and the learning log keeps segmenting.
CONTROLLER_IMPL = os.environ.get("MATTER_CONTROLLER_IMPL", "python-matter-server")

_TABLE = "matter_command_feedback"


def _pg() -> str:
    """PostgREST base URL (same convention as matter_removal.py)."""
    return os.environ.get("POSTGREST_URL", "http://postgrest:3001")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FeedbackAttempt(BaseModel):
    """Body for POST /api/matter/feedback — one command attempt, logged at send time."""
    node_id: int
    command: str                          # 'on' | 'off' | ...
    endpoint_id: int = 1
    device_label: Optional[str] = None
    api_success: bool                     # what the command endpoint reported
    api_detail: Optional[str] = None      # error detail when api_success=False


class FeedbackVerdict(BaseModel):
    """Body for PATCH /api/matter/feedback/{fid}/verdict — the operator's visual confirmation."""
    verdict: str = Field(pattern="^(worked|failed)$")   # 'unverified' is the default, never set explicitly
    notes: Optional[str] = None


@router.post("")
def log_attempt(body: FeedbackAttempt):
    """
    Insert one attempt row. Called by the UI as soon as the test command
    settles (success OR failure), BEFORE the operator answers the modal —
    so even never-answered attempts are on record as 'unverified'.
    """
    row = {
        "node_id": body.node_id,
        "endpoint_id": body.endpoint_id,
        "device_label": body.device_label,
        "command": body.command,
        "controller": CONTROLLER_IMPL,
        "api_success": body.api_success,
        "api_detail": body.api_detail,
        "sent_at": _now_iso(),
    }
    try:
        r = requests.post(
            f"{_pg()}/{_TABLE}",
            json=row,
            headers={"Prefer": "return=representation"},
            timeout=5,
        )
        r.raise_for_status()
        created = r.json()[0]
    except Exception as e:
        logger.error(f"matter feedback: attempt insert failed: {e}")
        raise HTTPException(status_code=502, detail=f"feedback insert failed: {e}")
    logger.info(
        f"matter feedback #{created['id']}: {body.command} node {body.node_id} "
        f"api_success={body.api_success} ({CONTROLLER_IMPL})"
    )
    return {"id": created["id"]}


@router.patch("/{fid}/verdict")
def set_verdict(fid: int, body: FeedbackVerdict):
    """
    Attach the operator's visual verdict to an attempt row. This is the
    user-input half of the learning loop — only 'worked'/'failed' are
    accepted; skipping the modal simply leaves the row 'unverified'.
    """
    patch = {
        "operator_verdict": body.verdict,
        "verdict_at": _now_iso(),
        "verdict_by": "operator",
        "notes": body.notes,
    }
    try:
        r = requests.patch(
            f"{_pg()}/{_TABLE}?id=eq.{fid}",
            json=patch,
            headers={"Prefer": "return=representation"},
            timeout=5,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        logger.error(f"matter feedback: verdict patch failed for #{fid}: {e}")
        raise HTTPException(status_code=502, detail=f"verdict update failed: {e}")
    if not rows:
        raise HTTPException(status_code=404, detail=f"feedback row {fid} not found")
    logger.info(f"matter feedback #{fid}: operator verdict = {body.verdict}")
    return {"id": fid, "operator_verdict": body.verdict}


@router.get("")
def list_feedback(node_id: Optional[int] = None, limit: int = 100):
    """Recent attempts, newest first. Optional node filter. For UI/agents/diagnosis."""
    q = f"{_pg()}/{_TABLE}?order=sent_at.desc&limit={min(max(limit, 1), 1000)}"
    if node_id is not None:
        q += f"&node_id=eq.{node_id}"
    try:
        r = requests.get(q, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"feedback query failed: {e}")


@router.get("/stats")
def feedback_stats():
    """
    Aggregate learning snapshot: per node AND per controller —
    attempts, operator-confirmed worked/failed, unverified, and the
    api-claimed-success-but-operator-said-failed divergence count
    (the number this table exists to expose).
    """
    try:
        r = requests.get(
            f"{_pg()}/{_TABLE}?select=node_id,device_label,controller,"
            f"command,api_success,operator_verdict&order=sent_at.desc&limit=5000",
            timeout=8,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"stats query failed: {e}")

    def _bucket():
        return {"attempts": 0, "worked": 0, "failed": 0, "unverified": 0,
                "api_ok_but_operator_failed": 0}

    by_node: dict = {}
    by_controller: dict = {}
    for row in rows:
        for key, agg in ((f"{row['node_id']}:{row.get('device_label') or '?'}", by_node),
                         (row.get("controller") or "?", by_controller)):
            b = agg.setdefault(key, _bucket())
            b["attempts"] += 1
            b[row["operator_verdict"]] += 1
            if row["api_success"] and row["operator_verdict"] == "failed":
                b["api_ok_but_operator_failed"] += 1
    return {"sample": len(rows), "by_node": by_node, "by_controller": by_controller}
