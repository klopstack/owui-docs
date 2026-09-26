"""docstree plugin: path-derivation safety + sweep reconciliation.

The plugin is loaded from its source file (it is not an importable package —
OWUI exec()s it). Its open_webui imports are lazy (inside _collect_desired),
so the module loads standalone and the pure helpers + sweep are testable with
_collect_desired monkeypatched.
"""
import asyncio
import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "docstree.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("docstree", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_plugin()


# ── path derivation (containment) ────────────────────────────────────────────

def test_desired_link_anchors_under_root(mod, tmp_path):
    root = tmp_path / "docs-set"
    root.mkdir()
    sp = str(root / "family" / "schoolwork" / "essay.md")
    dst = mod._desired_link(root, sp)
    assert dst == root / "family" / "schoolwork" / "essay.md"


def test_desired_link_rejects_protected_and_hidden(mod, tmp_path):
    root = tmp_path / "docs-set"
    root.mkdir()
    assert mod._desired_link(root, str(root / "by-date" / "2026" / "a.pdf")) is None
    assert mod._desired_link(root, str(root / "rejected" / "a.pdf")) is None
    assert mod._desired_link(root, str(root / ".hidden" / "a.pdf")) is None
    # A bare top-level name is degenerate (no <category>/<...> shape).
    assert mod._desired_link(root, str(root / "family")) is None


def test_desired_link_refuses_foreign_mount(mod, tmp_path):
    # A source_path outside our root signals a mount mismatch — refuse rather
    # than guess the category/stem boundary (unsafe for nested categories).
    root = tmp_path / "docs-set"
    root.mkdir()
    foreign = "/mnt/elsewhere/family/schoolwork/essay.md"
    assert mod._desired_link(root, foreign) is None


def test_master_under_root_requires_by_date(mod, tmp_path):
    root = tmp_path / "docs-set"
    root.mkdir()
    ok = str(root / "by-date" / "2026-01-01" / "a.pdf")
    assert mod._master_under_root(root, ok) == Path(ok)
    # A master outside by-date/ is refused (we only link from the archive).
    assert mod._master_under_root(root, str(root / "family" / "a.pdf")) is None
    assert mod._master_under_root(root, "/somewhere/else/a.pdf") is None


def test_upload_meta_reads_nested_data(mod):
    class F:
        meta = {"data": {"source_path": "/x/y/z.md", "external_ref": {"path": "/m"}}}
    um = mod._upload_meta(F())
    assert um["source_path"] == "/x/y/z.md"
    assert um["external_ref"]["path"] == "/m"


# ── link classification ──────────────────────────────────────────────────────

def test_link_kind_matrix(mod, tmp_path):
    master = tmp_path / "m.pdf"; master.write_bytes(b"M")
    dst = tmp_path / "c" / "m.pdf"
    dst.parent.mkdir(parents=True)

    assert mod._link_kind(dst, master) == "missing"

    os.symlink("/nonexistent", dst)
    assert mod._link_kind(dst, master) == "repair"

    dst.unlink(); os.symlink(master, dst)
    assert mod._link_kind(dst, master) == "converged"

    dst.unlink(); os.link(master, dst)
    assert mod._link_kind(dst, master) == "converged"

    dst.unlink(); dst.write_bytes(b"SOLE")  # nlink==1 regular file
    assert mod._link_kind(dst, master) == "refuse"


# ── sweep reconciliation (mocked desired set) ────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_sweep_creates_repairs_refuses_prunes(mod, tmp_path, monkeypatch):
    root = tmp_path / "docs-set"
    (root / "by-date" / "2026-01-01").mkdir(parents=True)
    master = root / "by-date" / "2026-01-01" / "a.pdf"
    master.write_bytes(b"M")

    # Desired: one link to create, one broken symlink to repair.
    want_new = root / "family" / "doc" / "a.pdf"
    want_fix = root / "family" / "doc" / "b.pdf"
    want_fix.parent.mkdir(parents=True)
    os.symlink("/nonexistent", want_fix)
    desired = {want_new: master, want_fix: master}

    # Orphan symlink (not in desired) → pruned. Sole copy (nlink==1) → refused.
    orphan = root / "family" / "gone" / "a.pdf"
    orphan.parent.mkdir(parents=True)
    os.symlink(master, orphan)
    sole = root / "family" / "keep" / "solo.pdf"
    sole.parent.mkdir(parents=True)
    sole.write_bytes(b"S")

    # A by-date file that is NOT in desired must never be pruned.
    (root / "by-date" / "2026-01-01" / "other.pdf").write_bytes(b"O")

    async def fake_collect(_root):
        return desired
    monkeypatch.setattr(mod, "_collect_desired", fake_collect)
    mod.valves.docs_set_root = str(root)

    stats = _run(mod._sweep_once())

    assert stats["created"] == 2            # want_new + want_fix (re-created)
    assert stats["repaired"] == 1           # want_fix (broken symlink unlinked)
    # samefile() works for both hardlinks and symlinks (realpath differs for
    # hardlinks — same inode, different paths).
    assert want_new.samefile(master)
    assert want_fix.samefile(master)
    assert stats["pruned"] == 1             # orphan
    assert not orphan.exists()
    assert stats["refused"] == 1            # sole copy
    assert sole.exists() and sole.read_bytes() == b"S"
    # by-date untouched.
    assert (root / "by-date" / "2026-01-01" / "other.pdf").exists()
    assert master.exists()


def test_sweep_refuses_regular_file_in_wanted_slot(mod, tmp_path, monkeypatch):
    root = tmp_path / "docs-set"
    (root / "by-date" / "2026-01-01").mkdir(parents=True)
    master = root / "by-date" / "2026-01-01" / "a.pdf"
    master.write_bytes(b"M")
    other = root / "by-date" / "2026-01-01" / "z.pdf"
    other.write_bytes(b"Z")
    # A hardlink to `other` squats in the wanted slot (nlink>1, not master).
    slot = root / "family" / "doc" / "a.pdf"
    slot.parent.mkdir(parents=True)
    os.link(other, slot)

    async def fake_collect(_root):
        return {slot: master}
    monkeypatch.setattr(mod, "_collect_desired", fake_collect)
    mod.valves.docs_set_root = str(root)

    stats = _run(mod._sweep_once())
    assert stats["refused"] == 1
    assert stats["created"] == 0
    assert slot.samefile(other)  # untouched


def test_sweep_noop_when_converged(mod, tmp_path, monkeypatch):
    root = tmp_path / "docs-set"
    (root / "by-date" / "2026-01-01").mkdir(parents=True)
    master = root / "by-date" / "2026-01-01" / "a.pdf"
    master.write_bytes(b"M")
    dst = root / "family" / "doc" / "a.pdf"
    dst.parent.mkdir(parents=True)
    os.link(master, dst)  # already converged

    async def fake_collect(_root):
        return {dst: master}
    monkeypatch.setattr(mod, "_collect_desired", fake_collect)
    mod.valves.docs_set_root = str(root)

    stats = _run(mod._sweep_once())
    assert stats == {"created": 0, "repaired": 0, "pruned": 0, "refused": 0, "errors": 0}
    assert dst.samefile(master)


def test_sweep_skips_when_root_missing(mod, tmp_path, monkeypatch):
    mod.valves.docs_set_root = str(tmp_path / "nope")
    stats = _run(mod._sweep_once())
    assert stats["created"] == 0
