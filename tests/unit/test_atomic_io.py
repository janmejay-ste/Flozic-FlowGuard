"""
Unit tests for utils/atomic_io — the atomic-swap writes that make parallel
chromium+webkit runs safe: a reader sees the old complete file or the new
complete file, never a partial one, and one engine's completed data is never
corrupted by the other engine writing its own.
"""
from __future__ import annotations

import json
import os

from utils.atomic_io import atomic_write_json, atomic_write_text


def test_write_and_content(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"a": 1, "nested": {"b": [1, 2]}})
    assert json.loads(p.read_text()) == {"a": 1, "nested": {"b": [1, 2]}}


def test_replaces_existing_completely(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write_text(p, "old content that is quite long " * 10)
    atomic_write_text(p, "new")
    assert p.read_text() == "new"          # no remnant of the longer old file


def test_no_tmp_files_left_behind(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"ok": True})
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []


def test_creates_parent_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "out.json"
    atomic_write_json(p, {"ok": True})
    assert p.is_file()


def test_failed_write_leaves_original_intact(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"version": 1})

    class Unserializable:
        pass

    try:
        atomic_write_json(p, {"bad": Unserializable()})
    except TypeError:
        pass
    # Original survives untouched and no tmp junk remains.
    assert json.loads(p.read_text()) == {"version": 1}
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []
