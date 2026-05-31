"""
Discoin Security System
========================

Institutional-grade threat detection, behavioral analysis, and graduated
enforcement engine.  Sits as a middle layer between the Discord bot and
the Dashboard API, consuming events from both via Redis pub/sub and
applying unified security policy.

Public API
----------
    SecurityEngine   -  central orchestrator (one per process)
    SecurityEvent    -  inbound event model
    SecurityVerdict  -  outcome of processing an event
"""
from __future__ import annotations

from security.engine import SecurityEngine
from security.models import SecurityEvent, SecurityVerdict

__all__ = ["SecurityEngine", "SecurityEvent", "SecurityVerdict"]
