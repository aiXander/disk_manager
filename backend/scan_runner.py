"""Background scan runner: one asyncio task per scan, any number of SSE subscribers.

``POST /scans`` starts a :class:`ScanRun`; ``GET /scans/{id}/stream`` only
subscribes. The runner keeps the latest ProgressEvent and a sequence number;
subscribers wait for the sequence to move and always read the newest
snapshot, so a slow or late subscriber never queues stale events, and a
subscriber that arrives after the scan finished gets the terminal event
immediately. Progress is published at most every ``min_interval`` seconds
(phase changes and the terminal event always go out).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import asdict
from typing import AsyncIterator

logger = logging.getLogger(__name__)

TERMINAL = ("complete", "cancelled", "error")


def _event_dict(event) -> dict:
    return asdict(event) if not isinstance(event, dict) else event


class ScanRun:
    def __init__(self, scan_id: str, roots: list[str], min_interval: float = 0.1) -> None:
        self.scan_id = scan_id
        self.roots = roots
        self.started_at = time.time()
        self.cancel_event = threading.Event()
        self.min_interval = min_interval
        self.last_event: dict | None = None
        self.task: asyncio.Task | None = None
        self._seq = 0
        self._changed = asyncio.Event()
        self._last_publish = 0.0

    # -- state -------------------------------------------------------------

    @property
    def status(self) -> str:
        return (self.last_event or {}).get("status", "running")

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL

    def cancel(self) -> None:
        self.cancel_event.set()

    # -- producer ----------------------------------------------------------

    def start(self, events: AsyncIterator) -> None:
        """Consume *events* (the scanner's async generator) in a background task."""
        self.task = asyncio.create_task(self._consume(events), name=f"scan-{self.scan_id}")

    async def _consume(self, events: AsyncIterator) -> None:
        try:
            async for event in events:
                self._on_event(_event_dict(event))
                if self.finished:
                    break
        except Exception as exc:  # scanner should yield its own error event; belt and braces
            logger.exception("Scan %s crashed", self.scan_id)
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            if not self.finished:
                self._fail("scan ended without a terminal event")

    def _fail(self, msg: str) -> None:
        base = dict(self.last_event or {"scan_id": self.scan_id, "files_scanned": 0,
                                         "dirs_scanned": 0, "current_path": "", "bytes_seen": 0})
        base.update(status="error", error=msg)
        self._publish(base)

    def _on_event(self, ev: dict) -> None:
        prev_phase = (self.last_event or {}).get("phase")
        self.last_event = ev
        now = time.monotonic()
        if (
            ev.get("status") in TERMINAL
            or ev.get("phase") != prev_phase
            or now - self._last_publish >= self.min_interval
        ):
            self._publish(ev)

    def _publish(self, ev: dict) -> None:
        self.last_event = ev
        self._last_publish = time.monotonic()
        self._seq += 1
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    # -- subscribers -------------------------------------------------------

    async def subscribe(self, keepalive: float = 15.0) -> AsyncIterator[str]:
        """Yield SSE frames until (and including) the terminal event."""
        seen = 0
        while True:
            if self._seq != seen:
                seen = self._seq
                ev = self.last_event
                kind = ev["status"] if ev["status"] in TERMINAL else "progress"
                yield f"event: {kind}\ndata: {json.dumps(ev)}\n\n"
                if kind != "progress":
                    return
                continue
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), timeout=keepalive)
            except TimeoutError:
                yield ": keepalive\n\n"


def terminal_frame(meta: dict) -> str:
    """SSE terminal frame for a scan with no live runner (finished in a previous server run)."""
    status = meta["status"]
    kind = status if status in TERMINAL else "error"
    ev = {
        "scan_id": meta["scan_id"],
        "files_scanned": meta.get("total_files", 0),
        "dirs_scanned": meta.get("total_dirs", 0),
        "current_path": "",
        "bytes_seen": meta.get("total_bytes", 0),
        "status": kind,
        "error": meta.get("error_msg") or (None if kind != "error" else f"scan {status}"),
        "phase": "finalizing",
        "elapsed_s": (meta.get("finished_at") or 0) - (meta.get("started_at") or 0)
        if meta.get("finished_at") else 0.0,
        "errors": 0,
    }
    return f"event: {kind}\ndata: {json.dumps(ev)}\n\n"
