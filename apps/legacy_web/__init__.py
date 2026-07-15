"""legacy_web — the /legacy/* Jinja+jQuery surface (strangler-fig transitional).

The RN admin app is the front door at GET /. Every legacy page lives under
/legacy/<surface> and burns down per-surface as the RN port advances (cutover
plan: docs/plans/full_app_react_native_cutover_*.md). ARCHIVE at parity, never
delete (Architect ruling Q5, 2026-07-14): this package is removed only after a
production burn-in on RN, via an explicit operator decision.
"""
from .router import build_legacy_router, LEGACY_SURFACES  # noqa: F401
