"""
Event helper. Every stage reports progress through emit(), which saves
the event (for history and replay) and, if the live view is watching,
tells it to redraw that stage straight away.
"""
from __future__ import annotations

import threading
from typing import Callable

from core.db import add_event
from core.models import EventType, RunEvent, Stage

_local = threading.local()  # one listener per Streamlit session thread


def set_listener(fn: Callable[[RunEvent], None] | None) -> None:
    _local.listener = fn


def emit(run_id: str, stage: Stage, type: EventType, message: str,
         data: dict | None = None) -> RunEvent:
    event = RunEvent(run_id=run_id, stage=stage, type=type,
                     message=message, data=data or {})
    add_event(event)
    listener = getattr(_local, "listener", None)
    if listener is not None:
        listener(event)
    return event
