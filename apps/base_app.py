"""
Backwards-compatible re-export for BaseApp.

The implementation has been split into focused modules under apps/base/.
This stub preserves all existing import paths:
    from apps.base_app import BaseApp
"""

from apps.base.core import BaseApp

__all__ = ['BaseApp']
