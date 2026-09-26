"""Tests for the docbrowser plugin core (pure-Python parts).

Runnable without Open WebUI installed: the plugin lazy-imports OWUI models
inside the sweep functions, so importing the module only needs pydantic.
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import docbrowser as db  # noqa: E402


# ── doc key / role derivation ────────────────────────────────────────────────

@pytest.mark.parametrize("name,key", [
    ("20260925-143022_layered.pdf", "20260925-143022"),
    ("20260925-143022.md", "20260925-143022"),
    ("20260925-143022.json", "20260925-143022"),
    ("20260925-143022_orig_front.pdf", "20260925-143022"),
    ("20260925-143022_orig_back.pdf", "20260925-143022"),
    ("20260925-143022_orig.pdf", "20260925-143022"),
    ("20260925-143022_audio.mp3", "20260925-143022"),
    # same-second collision suffix is PART of the key
    ("20260925-143022-abc12345_layered.pdf", "20260925-143022-abc12345"),
    ("20260925-143022-abc12345.md", "20260925-143022-abc12345"),
])
def test_doc_key_from_name(name, key):
    assert db.doc_key_from_name(name) == key


@pytest.mark.parametrize("name,role", [
    ("20260925-143022_orig_front.pdf", "front_orig"),
    ("20260925-143022_orig_back.pdf", "back_orig"),
    ("20260925-143022_collated.pdf", "collated"),
    ("20260925-143022_layered.pdf", "layered"),
    ("20260925-143022_audio.mp3", "audio"),
    ("20260925-143022_orig.pdf", "orig"),
    ("20260925-143022.md", "markdown"),
    ("20260925-143022.json", "ocr_raw"),
    ("20260925-143022_thumb.jpg", "thumbnail"),
])
def test_role_for_name(name, role):
    assert db.role_for_name(name) == role


def test_is_sibling_separator_rules():
    # exact doc: matches its own files
    assert db.is_sibling("20260925-143022", "20260925-143022.md")
    assert db.is_sibling("20260925-143022", "20260925-143022_layered.pdf")
    # the plain doc must NOT claim the collision-suffixed doc's files
    assert not db.is_sibling("20260925-143022", "20260925-143022-abc12345.md")
    # and vice versa
    assert not db.is_sibling("20260925-143022-abc12345", "20260925-143022.md")
    # unrelated doc
    assert not db.is_sibling("20260925-143022", "20260925-143023.md")
    # a doc key that is a bare prefix of another timestamp
    assert not db.is_sibling("20260925-1430", "20260925-143022.md")


# ── doc_meta synthesis ───────────────────────────────────────────────────────

def _make_docs_set(tmp_path: Path, doc_key: str = "20260925-143022") -> Path:
    root = tmp_path / "docs-set"
    date_dir = root / "by-date" / "2026-09-25"
    date_dir.mkdir(parents=True)
    (date_dir / f"{doc_key}_orig_front.pdf").write_bytes(b"%PDF-front")
    (date_dir / f"{doc_key}_orig_back.pdf").write_bytes(b"%PDF-back")
    (date_dir / f"{doc_key}_layered.pdf").write_bytes(b"%PDF-layered")
    (date_dir / f"{doc_key}.md").write_text("# hi")
    # a DIFFERENT doc in the same date dir must not leak in
    (date_dir / "20260925-143023.md").write_text("# other")
    return root


def test_synthesize_doc_meta_full(tmp_path, monkeypatch):
    root = _make_docs_set(tmp_path)
    master = root / "by-date" / "2026-09-25" / "20260925-143022_layered.pdf"
    monkeypatch.setattr(db, "_pdf_page_count", lambda p: 12)
    ref_ids = {str(master.resolve()): "f_layered"}
    meta = db.synthesize_doc_meta(
        doc_key="20260925-143022",
        source_path=str(root / "family" / "schoolwork" / "my-title" / "20260925-143022.md"),
        master_path=master,
        root=root,
        ref_file_ids=ref_ids,
        kb_file_id="f_md",
    )
    assert meta["schema"] == 1
    assert meta["doc_key"] == "20260925-143022"
    assert meta["title"] == "my-title"
    assert meta["category"] == "family/schoolwork"
    assert meta["primary"]["role"] == "layered"
    assert meta["primary"]["file_id"] == "f_layered"
    assert meta["primary"]["extent"] == {"unit": "pages", "value": 12}
    assert meta["markdown"]["file_id"] is None  # md row not in ref map
    roles = {a["role"] for a in meta["artifacts"]}
    assert roles == {"front_orig", "back_orig", "layered", "markdown"}
    # the other doc's file did not leak in
    assert all("143023" not in a["relpath"] for a in meta["artifacts"])
    assert meta["status"] == "done"


def test_synthesize_prefers_layered_over_orig(tmp_path, monkeypatch):
    root = _make_docs_set(tmp_path)
    master = root / "by-date" / "2026-09-25" / "20260925-143022_layered.pdf"
    monkeypatch.setattr(db, "_pdf_page_count", lambda p: 3)
    meta = db.synthesize_doc_meta(
        doc_key="20260925-143022",
        source_path=str(root / "family" / "my-title" / "20260925-143022.md"),
        master_path=master, root=root, ref_file_ids={}, kb_file_id="f_md",
    )
    assert meta["primary"]["role"] == "layered"


def test_synthesize_falls_back_to_orig_without_layered(tmp_path, monkeypatch):
    root = _make_docs_set(tmp_path)
    (root / "by-date" / "2026-09-25" / "20260925-143022_layered.pdf").unlink()
    master = root / "by-date" / "2026-09-25" / "20260925-143022_orig_front.pdf"
    monkeypatch.setattr(db, "_pdf_page_count", lambda p: 1)
    meta = db.synthesize_doc_meta(
        doc_key="20260925-143022",
        source_path=str(root / "family" / "my-title" / "20260925-143022.md"),
        master_path=master, root=root, ref_file_ids={}, kb_file_id="f_md",
    )
    # no layered → no orig (front/back are not "orig") → primary is None
    assert meta["primary"] is None
    assert meta["media_type"] is None


def test_synthesize_carries_forward_prior_status(tmp_path, monkeypatch):
    root = _make_docs_set(tmp_path)
    master = root / "by-date" / "2026-09-25" / "20260925-143022_layered.pdf"
    monkeypatch.setattr(db, "_pdf_page_count", lambda p: 2)
    prior = {"schema": 1, "doc_key": "20260925-143022", "status": "corrected",
             "flags": ["garbled-ocr"], "correction_sha": "abc"}
    meta = db.synthesize_doc_meta(
        doc_key="20260925-143022",
        source_path=str(root / "family" / "my-title" / "20260925-143022.md"),
        master_path=master, root=root, ref_file_ids={}, kb_file_id="f_md",
        prior=prior,
    )
    assert meta["status"] == "corrected"
    assert meta["flags"] == ["garbled-ocr"]
    assert meta["correction_sha"] == "abc"


def test_extent_null_when_pypdf_missing(tmp_path, monkeypatch):
    root = _make_docs_set(tmp_path)
    master = root / "by-date" / "2026-09-25" / "20260925-143022_layered.pdf"
    monkeypatch.setattr(db, "_pdf_page_count", lambda p: None)
    meta = db.synthesize_doc_meta(
        doc_key="20260925-143022",
        source_path=str(root / "family" / "my-title" / "20260925-143022.md"),
        master_path=master, root=root, ref_file_ids={}, kb_file_id="f_md",
    )
    assert meta["primary"]["extent"] is None


# ── correction store ─────────────────────────────────────────────────────────

def test_correction_round_trip(tmp_path):
    root = tmp_path / "docs-set"
    doc_key = "20260925-143022"
    assert db.read_correction(root, doc_key) is None
    p = db.correction_path(root, doc_key)
    p.parent.mkdir(parents=True)
    corr = {"schema": 1, "doc_key": doc_key, "corrected_markdown": "# fixed",
            "note": "typo", "created_at": 1.0, "author": "benklop"}
    p.write_text(json.dumps(corr))
    got = db.read_correction(root, doc_key)
    assert got == corr
    assert db.correction_sha(got) == db.correction_sha(corr)
    assert db.correction_sha(None) is None


def test_correction_wrong_doc_key_rejected(tmp_path):
    root = tmp_path / "docs-set"
    p = db.correction_path(root, "20260925-143022")
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"schema": 1, "doc_key": "OTHER", "note": "x"}))
    assert db.read_correction(root, "20260925-143022") is None


def test_correction_corrupt_file_returns_none(tmp_path):
    root = tmp_path / "docs-set"
    p = db.correction_path(root, "20260925-143022")
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert db.read_correction(root, "20260925-143022") is None


# ── link-tree invariants (absorbed from docstree) ────────────────────────────

def test_desired_link_requires_two_levels_under_root(tmp_path):
    root = tmp_path / "docs-set"
    assert db._desired_link(root, str(root / "family" / "t" / "a.md")) == \
        root / "family" / "t" / "a.md"
    # bare top-level name → refuse
    assert db._desired_link(root, str(root / "a.md")) is None
    # outside root → refuse
    assert db._desired_link(root, "/elsewhere/a.md") is None
    # by-date/ is never managed
    assert db._desired_link(root, str(root / "by-date" / "x" / "a.md")) is None
    # _reviews/ is never managed
    assert db._desired_link(root, str(root / "_reviews" / "x" / "a.md")) is None


def test_master_under_root_only_by_date(tmp_path):
    root = tmp_path / "docs-set"
    assert db._master_under_root(root, str(root / "by-date" / "d" / "a.pdf")) is not None
    assert db._master_under_root(root, str(root / "family" / "a.pdf")) is None
    assert db._master_under_root(root, "/elsewhere/a.pdf") is None


def test_link_kind_and_materialize(tmp_path):
    root = tmp_path / "docs-set"
    master = root / "by-date" / "d" / "a.pdf"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"%PDF")
    dst = root / "family" / "t" / "a.pdf"

    assert db._link_kind(dst, master) == "missing"
    kind = db._link_or_symlink(master, dst)
    assert kind in ("link", "symlink")
    assert db._link_kind(dst, master) == "converged"

    # a SOLE-COPY regular file occupying the slot is never touched
    dst2 = root / "family" / "t2" / "b.pdf"
    dst2.parent.mkdir(parents=True)
    dst2.write_bytes(b"sole")
    assert db._link_kind(dst2, master) == "refuse"

    # a hardlink to something ELSE is a repair candidate
    other = root / "by-date" / "d" / "other.pdf"
    other.write_bytes(b"%PDF-other")
    dst3 = root / "family" / "t3" / "c.pdf"
    dst3.parent.mkdir(parents=True)
    os.link(other, dst3)
    assert db._link_kind(dst3, master) == "repair"


def test_upload_meta_nested_and_flat():
    class F:
        meta = {"data": {"source_path": "/x", "external_ref": {"path": "/y"}}}
    out = db._upload_meta(F())
    assert out["source_path"] == "/x"
    assert out["external_ref"]["path"] == "/y"

    class G:
        meta = {"source_path": "/x2"}
    assert db._upload_meta(G())["source_path"] == "/x2"

    class H:
        meta = None
    assert db._upload_meta(H()) == {}
