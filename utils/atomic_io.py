"""
Atomic file writes — so a concurrent reader NEVER sees a half-written file.

Why: chromium and webkit runs execute in PARALLEL. Each engine's teardown
rebuilds the combined report by READING the other engine's persisted files
(sidecar, snapshot, mobile-summary). A plain write_text() truncates then
writes, so a rebuild that fires mid-write could read partial JSON. Writing to
a temp file in the same directory and os.replace()-ing it in is atomic on
POSIX: a reader observes either the old complete file or the new complete
file — one engine's finished data can never be corrupted or half-seen while
the other engine is writing its own.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, obj: Any, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent))
