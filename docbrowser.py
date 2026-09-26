"""
title: Docbrowser
author: benklop
version: 1.0
description: Consolidated document meta-entries for the docs-pipeline KBs.
  Absorbs the former docstree link-tree maintenance and adds: (1) doc_meta
  adoption — synthesizes a media-generic document metadata block (doc_key,
  title, category, primary artifact + extent, artifact list) from by-date
  siblings and OWUI reference file rows, written into the KB file's
  meta.data.doc_meta; (2) a metadata guardian (alerts on dangling by-date
  masters); (3) review/correction groundwork — watches <docs_set>/_reviews/
  <doc_key>/correction.json and stamps doc_meta.status. Reference files
  (external_ref.path pointing at by-date masters) get a hardlink — or a
  relative symlink when hardlinks are impossible — into the category tree so
  the KB directory layout is browsable on the filesystem. A periodic sweep
  is the source of truth; knowledge.file.* / file.deleted events are
  debounced hints that trigger an early sweep. Safety: only ever creates or
  unlinks paths under <category>/<stem>/; by-date/, rejected/ and _reviews/
  are never touched, and nlink==1 regular files are never modified (sole
  copies are sacred).
required_open_webui_version: 0.11.4
"""

import asyncio
import hashlib
import json
import logging
import os
import stat
import time
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger("docbrowser")

# Events that mean "the KB membership changed" → schedule an early sweep.
_HINT_EVENTS = frozenset({
    'knowledge.file.added',
    'knowledge.file.updated',
    'knowledge.file.removed',
    'knowledge.file.moved',
    'knowledge.reset',
    'knowledge.reindexed',
    'file.deleted',
})

# Top-level dirs under docs-set that the link-tree sweep must never touch.
_NEVER_TOUCH_TOP_DIRS = frozenset({'by-date', 'rejected', '_reviews'})


class Valves(BaseModel):
    # Where docs-set is mounted INSIDE the open-webui container.
    docs_set_root: str = Field(
        default='/mnt/docs-set',
        description='docs-set mount path inside the open-webui container',
    )
    # Full-sweep cadence (seconds). Events only shorten the wait, they never
    # extend it.
    sweep_interval_sec: int = Field(
        default=60, ge=5,
        description='Periodic full-sweep interval (seconds)',
    )
    # Debounce window for event-triggered sweeps (seconds).
    event_debounce_sec: float = Field(
        default=5.0, ge=0,
        description='Debounce window for event-triggered sweeps (seconds)',
    )
    # Set false to disable event hints (pure periodic sweep).
    enable_events: bool = Field(
        default=True,
        description='React to knowledge.file.* / file.deleted events (hints)',
    )


valves = Valves()


# ═══════════════════════════════════════════════════════════════════════════
# doc_meta contract (frozen — consumed by the OWUI fork frontend)
# ═══════════════════════════════════════════════════════════════════════════
#
# Written into the KB file's upload metadata: file.meta.data.doc_meta.
# Schema:
# {
#   "schema": 1,
#   "doc_key": "20260925-143022",        # by-date basename stem (incl. -sha8)
#   "title": "my-title",                  # from source_path parent dir
#   "category": "family/schoolwork",      # from source_path (root-relative)
#   "media_type": "application/pdf",      # of the primary artifact
#   "primary": {
#     "role": "layered",
#     "file_id": "f_...",                 # OWUI row → /files/{id}/content
#     "relpath": "by-date/2026-09-25/20260925-143022_layered.pdf",
#     "media_type": "application/pdf",
#     "extent": {"unit": "pages", "value": 12}   # or null (audio: deferred)
#   },
#   "markdown": {"file_id": "f_...", "relpath": "..."},
#   "artifacts": [
#     {"role": "layered", "file_id": "f_...", "relpath": "...",
#      "media_type": "application/pdf"},
#     {"role": "front_orig", "file_id": null, "relpath": "...",
#      "media_type": "application/pdf"}
#   ],
#   "status": "done",                     # "done" | "corrected"
#   "flags": [],
#   "correction_sha": null                # sha256 of the applied correction
# }
DOC_META_SCHEMA = 1

# Role suffixes, LONGEST FIRST (a name matches at most one). Applied to the
# by-date basename stem (extension stripped). Mirrors the pipeline's
# _role_for conventions (docs-pipeline/docs_pipeline/pipeline.py).
_ROLE_SUFFIXES = (
    ("_orig_front", "front_orig"),
    ("_orig_back", "back_orig"),
    ("_collated", "collated"),
    ("_layered", "layered"),
    ("_audio", "audio"),
    ("_orig", "orig"),
)

# Primary-artifact preference: the download/preview target. Media-generic —
# the audio lane slots in here without touching the contract.
_PRIMARY_ROLES = ("layered", "audio", "orig")

_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def doc_key_from_name(name: str) -> str:
    """The document key for a by-date basename.

    "20260925-143022_layered.pdf" → "20260925-143022"
    "20260925-143022-abc12345.md" → "20260925-143025-abc12345" (collision
    suffix is PART of the key)
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for suf, _role in _ROLE_SUFFIXES:
        if stem.endswith(suf):
            return stem[: -len(suf)]
    return stem


def role_for_name(name: str) -> str:
    """Artifact role for a by-date basename (mirrors pipeline _role_for)."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for suf, role in _ROLE_SUFFIXES:
        if stem.endswith(suf):
            return role
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext == "md":
        return "markdown"
    if ext == "json":
        return "ocr_raw"
    if ext in ("jpg", "jpeg", "png"):
        return "thumbnail"
    return "other"


def is_sibling(doc_key: str, name: str) -> bool:
    """True if by-date basename `name` belongs to document `doc_key`.

    The key must be followed by a '.' or '_' separator — this is what keeps
    a collision-suffixed doc ("...-abc12345") from claiming the plain doc's
    files (and vice versa).
    """
    if not name.startswith(doc_key):
        return False
    rest = name[len(doc_key):]
    return len(rest) > 0 and rest[0] in (".", "_")


def media_type_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _MEDIA_TYPES.get(ext, "application/octet-stream")


def _title_and_category(source_path: str, root: Path) -> tuple[str, str]:
    """(title, category) from the source_path citation metadata.

    source_path = <root>/<category...>/<title_stem>/<name>; the title is the
    parent dir name, the category the root-relative path above it.
    """
    sp = Path(source_path)
    try:
        rel = sp.relative_to(root)
    except ValueError:
        return "", ""
    if len(rel.parts) < 3:
        return "", ""
    return rel.parts[-2], "/".join(rel.parts[:-2])


def _pdf_page_count(path: Path) -> int | None:
    """Page count of a PDF via pypdf (present in the OWUI image). None on
    any failure — the extent column renders '—' rather than crashing."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def _extent_for(role: str, path: Path) -> dict | None:
    """Media-generic extent of an artifact. PDFs: page count. Audio/video:
    deferred to the respective lanes (null until then)."""
    if role in ("layered", "collated", "orig", "front_orig", "back_orig"):
        if path.suffix.lower() == ".pdf":
            n = _pdf_page_count(path)
            if n is not None:
                return {"unit": "pages", "value": n}
    return None


def synthesize_doc_meta(
    *,
    doc_key: str,
    source_path: str,
    master_path: Path,
    root: Path,
    ref_file_ids: dict[str, str],
    kb_file_id: str,
    prior: dict | None = None,
) -> dict:
    """Pure synthesis of a doc_meta block from by-date siblings.

    `ref_file_ids` maps absolute by-date path → OWUI file id (from the
    `ref:` rows in the file table). `prior` is the existing doc_meta (its
    status/flags/correction_sha are carried forward).
    """
    date_dir = master_path.parent
    siblings: list[dict] = []
    try:
        entries = sorted(date_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        entries = []
    for p in entries:
        if not p.is_file() or not is_sibling(doc_key, p.name):
            continue
        role = role_for_name(p.name)
        if role == "other":
            continue
        abs_path = str(p.resolve())
        siblings.append({
            "role": role,
            "file_id": ref_file_ids.get(abs_path),
            "relpath": str(p.relative_to(root)),
            "media_type": media_type_for(p.name),
        })

    primary = None
    for want in _PRIMARY_ROLES:
        hit = next((s for s in siblings if s["role"] == want), None)
        if hit:
            primary = {
                "role": hit["role"],
                "file_id": hit["file_id"],
                "relpath": hit["relpath"],
                "media_type": hit["media_type"],
                "extent": _extent_for(hit["role"], date_dir / hit["relpath"].rsplit("/", 1)[-1]),
            }
            break

    md = next((s for s in siblings if s["role"] == "markdown"), None)
    title, category = _title_and_category(source_path, root)

    meta = {
        "schema": DOC_META_SCHEMA,
        "doc_key": doc_key,
        "title": title,
        "category": category,
        "media_type": primary["media_type"] if primary else None,
        "primary": primary,
        "markdown": (
            {"file_id": md["file_id"], "relpath": md["relpath"]} if md else None
        ),
        "artifacts": siblings,
        "status": "done",
        "flags": [],
        "correction_sha": None,
    }
    # Carry forward review state from a prior adoption (the sweep's
    # correction pass re-applies it afterwards).
    if isinstance(prior, dict):
        for key in ("status", "flags", "correction_sha"):
            if key in prior:
                meta[key] = prior[key]
    return meta


# ═══════════════════════════════════════════════════════════════════════════
# Link-tree maintenance (absorbed verbatim from the docstree plugin)
# ═══════════════════════════════════════════════════════════════════════════

def _is_managed_top(rel: Path) -> bool:
    """True if rel is a category path we own (not by-date/, rejected/,
    _reviews/, hidden)."""
    if not rel.parts or ".." in rel.parts:
        return False
    top = rel.parts[0]
    return top not in _NEVER_TOUCH_TOP_DIRS and not top.startswith(".")


def _desired_link(root: Path, source_path: str) -> Path | None:
    """Derive the category-tree link path from a file's source_path metadata.

    source_path is the absolute <root>/<category...>/<stem>/<name> path
    recorded by `oikb add --source-path`. Both the pipeline and this plugin
    mount docs-set at the SAME path, so source_path is already anchored under
    our root and the desired link is source_path itself. We refuse (return
    None) anything not strictly under root — a foreign path signals a mount
    mismatch, and guessing the category/stem boundary would be unsafe for
    nested categories (e.g. family/schoolwork/<stem>/<name>).
    """
    sp = Path(source_path)
    try:
        rel = sp.relative_to(root)
    except ValueError:
        return None  # not under our root — refuse rather than guess
    # A managed entry is always <category>/<something>/... — reject a bare
    # top-level name (degenerate source_path).
    if len(rel.parts) < 2:
        return None
    if not _is_managed_top(rel):
        return None
    return root / rel


def _master_under_root(root: Path, master: str) -> Path | None:
    """Validate an external_ref.path (by-date master) sits under our root."""
    mp = Path(master)
    try:
        rel = mp.relative_to(root)
    except ValueError:
        return None
    if not rel.parts or rel.parts[0] not in _NEVER_TOUCH_TOP_DIRS:
        # Masters live in by-date/; refuse to link from anywhere else.
        return None
    return mp


def _upload_meta(file) -> dict:
    """The upload-time metadata (source_path, external_ref, ...) as stored in
    file.meta — nested under the 'data' key by the upload handler, but also
    peek at the top level in case a future version flattens it."""
    meta = file.meta or {}
    if not isinstance(meta, dict):
        return {}
    inner = meta.get('data')
    out = dict(inner) if isinstance(inner, dict) else {}
    for key in ('source_path', 'external_ref'):
        if key not in out and key in meta:
            out[key] = meta[key]
    return out


def _link_kind(dst: Path, master: Path) -> str:
    """Classify dst relative to master:
    'converged' | 'repair' | 'refuse' | 'missing'
    """
    if not dst.exists() and not dst.is_symlink():
        return 'missing'
    if dst.is_symlink():
        try:
            return 'converged' if os.path.realpath(dst) == os.path.realpath(master) else 'repair'
        except OSError:
            return 'repair'
    try:
        st = dst.lstat()
    except OSError:
        return 'missing'
    if stat.S_ISREG(st.st_mode):
        if st.st_nlink == 1:
            return 'refuse'  # sole copy — never touch
        try:
            return 'converged' if dst.samefile(master) else 'repair'
        except OSError:
            return 'repair'
    return 'refuse'  # fifo/socket/device — leave alone


def _link_or_symlink(src: Path, dst: Path) -> str:
    """Hardlink, else RELATIVE symlink, else raise (copy is retired)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return 'link'
    except OSError:
        target = os.path.relpath(os.path.abspath(src), os.path.dirname(os.path.abspath(dst)))
        os.symlink(target, dst)
        return 'symlink'


# ═══════════════════════════════════════════════════════════════════════════
# Correction store (review/correction groundwork — no UI in this version)
# ═══════════════════════════════════════════════════════════════════════════
#
# Convention: <docs_set>/_reviews/<doc_key>/correction.json
# {
#   "schema": 1,
#   "doc_key": "...",
#   "corrected_markdown": "...",   # optional
#   "title": "...",                 # optional
#   "category": "...",              # optional
#   "note": "...",
#   "created_at": 1758800000.0,
#   "author": "..."
# }
# The sweep detects new/changed correction files and stamps
# doc_meta.status="corrected" (+ correction_sha) on the KB file. Consuming
# the corrected content is the NEXT plan's work.

_REVIEW_DIRNAME = "_reviews"
_CORRECTION_FILENAME = "correction.json"


def correction_path(root: Path, doc_key: str) -> Path:
    return root / _REVIEW_DIRNAME / doc_key / _CORRECTION_FILENAME


def read_correction(root: Path, doc_key: str) -> dict | None:
    """Parse the correction file for a doc; None if absent/invalid."""
    p = correction_path(root, doc_key)
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warning("docbrowser: unreadable correction at %s", p)
        return None
    if not isinstance(obj, dict) or obj.get("doc_key") != doc_key:
        return None
    return obj


def correction_sha(correction: dict | None) -> str | None:
    if correction is None:
        return None
    canon = json.dumps(correction, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════
# Sweep: one reconciliation pass (tree links + adoption + corrections +
# guardian). Idempotent; per-item errors are logged and skipped so one bad
# file never aborts the sweep.
# ═══════════════════════════════════════════════════════════════════════════

async def _collect_kb_files(root: Path) -> list[dict]:
    """Walk every KB's join table → [{file, source_path, master, desired}].

    Truth = the knowledge_file join table (cheap, precise). Only reference
    files (external_ref.path) participate — byte-uploaded files live in OWUI
    storage, not docs-set, so they have no tree representation.
    """
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.knowledge import Knowledges

    out: list[dict] = []
    async with get_async_db_context() as db:
        kbs = await Knowledges.get_knowledge_bases(db=db)
        for kb in kbs:
            try:
                pairs = await Knowledges.get_files_with_directory_ids(kb.id, db=db)
            except Exception:
                log.exception('docbrowser: could not list files for KB %s', kb.id)
                continue
            for file, _dir_id in pairs:
                try:
                    umeta = _upload_meta(file)
                    source_path = umeta.get('source_path')
                    ext_ref = umeta.get('external_ref') or {}
                    master = ext_ref.get('path') if isinstance(ext_ref, dict) else None
                    if not source_path or not master:
                        continue
                    out.append({
                        'file': file,
                        'source_path': source_path,
                        'master': master,
                        'desired': _desired_link(root, source_path),
                    })
                except Exception:
                    log.exception('docbrowser: bad metadata for file %s',
                                  getattr(file, 'id', '?'))
    return out


async def _ref_file_id_map() -> dict[str, str]:
    """Map absolute by-date path → OWUI file id for all reference rows."""
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.files import File
    from sqlalchemy import select

    out: dict[str, str] = {}
    async with get_async_db_context() as db:
        res = await db.execute(select(File.id, File.path).where(File.path.like('ref:%')))
        for fid, fpath in res.all():
            if isinstance(fpath, str) and fpath.startswith('ref:'):
                out[fpath[len('ref:'):]] = fid
    return out


async def _write_doc_meta(file_id: str, doc_meta: dict) -> None:
    """Merge doc_meta into file.meta.data (preserving source_path,
    external_ref, ...). Direct in-process write — same privilege class the
    plugin already uses for reads; confined to the meta JSON column."""
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.files import File
    from sqlalchemy import select

    async with get_async_db_context() as db:
        res = await db.execute(select(File).where(File.id == file_id))
        row = res.scalars().first()
        if row is None:
            return
        meta = dict(row.meta or {})
        data = dict(meta.get('data') or {})
        data['doc_meta'] = doc_meta
        meta['data'] = data
        row.meta = meta
        row.updated_at = int(time.time())
        await db.commit()


async def _sweep_once() -> dict:
    root = Path(valves.docs_set_root)
    stats = {'links_created': 0, 'links_repaired': 0, 'links_pruned': 0,
             'links_refused': 0, 'adopted': 0, 'guardian_dangling': 0,
             'corrections_applied': 0, 'errors': 0}
    if not root.is_dir():
        log.warning('docbrowser: root %s missing — sweep skipped', root)
        return stats

    kb_files = await _collect_kb_files(root)
    ref_ids = await _ref_file_id_map()

    # 1. Link tree: materialize missing / repair wrong links.
    desired: dict[Path, Path] = {}
    for entry in kb_files:
        master_p = _master_under_root(root, entry['master'])
        if entry['desired'] is None or master_p is None:
            continue
        desired[entry['desired']] = master_p
        if not master_p.is_file():
            # Dangling master (removed out-of-band) — guardian alert.
            stats['guardian_dangling'] += 1
            log.warning('docbrowser: DANGLING master for %s (%s)',
                        entry['file'].id, entry['master'])
            continue
        try:
            dst = entry['desired']
            kind = _link_kind(dst, master_p)
            if kind == 'converged':
                continue
            if kind == 'refuse':
                stats['links_refused'] += 1
                log.warning('docbrowser: refusing to touch %s (sole copy)', dst)
                continue
            if kind in ('repair', 'missing'):
                if dst.is_symlink():
                    dst.unlink()
                    stats['links_repaired'] += 1
                elif dst.exists():
                    # Regular file (nlink>1, i.e. a hardlink to something
                    # else) squatting in the slot — os.link would raise
                    # FileExistsError, so refuse rather than clobber.
                    stats['links_refused'] += 1
                    log.warning('docbrowser: refusing to overwrite %s (regular file)', dst)
                    continue
                _link_or_symlink(master_p, dst)
                stats['links_created'] += 1
        except Exception:
            stats['errors'] += 1
            log.exception('docbrowser: failed to materialize %s', entry['desired'])

    # 2. doc_meta adoption: one block per reference KB file.
    for entry in kb_files:
        try:
            master_p = Path(entry['master'])
            if not master_p.is_file():
                continue  # already alerted by the guardian
            doc_key = doc_key_from_name(master_p.name)
            prior = (_upload_meta(entry['file']).get('doc_meta')) or None
            meta = synthesize_doc_meta(
                doc_key=doc_key,
                source_path=entry['source_path'],
                master_path=master_p,
                root=root,
                ref_file_ids=ref_ids,
                kb_file_id=entry['file'].id,
                prior=prior,
            )
            if prior != meta:
                await _write_doc_meta(entry['file'].id, meta)
                stats['adopted'] += 1
        except Exception:
            stats['errors'] += 1
            log.exception('docbrowser: adoption failed for file %s',
                          getattr(entry['file'], 'id', '?'))

    # 3. Corrections: stamp status from the _reviews/ store.
    reviews_dir = root / _REVIEW_DIRNAME
    if reviews_dir.is_dir():
        try:
            review_dirs = sorted(p for p in reviews_dir.iterdir() if p.is_dir())
        except OSError:
            review_dirs = []
        by_key: dict[str, dict] = {}
        for entry in kb_files:
            master_p = Path(entry['master'])
            by_key.setdefault(doc_key_from_name(master_p.name), entry)
        for rd in review_dirs:
            doc_key = rd.name
            entry = by_key.get(doc_key)
            if entry is None:
                continue  # correction for a doc not (currently) in any KB
            try:
                corr = read_correction(root, doc_key)
                sha = correction_sha(corr)
                prior = (_upload_meta(entry['file']).get('doc_meta')) or {}
                if prior.get('correction_sha') == sha:
                    continue
                prior['status'] = 'corrected' if corr is not None else 'done'
                prior['correction_sha'] = sha
                await _write_doc_meta(entry['file'].id, prior)
                stats['corrections_applied'] += 1
                log.info('docbrowser: correction %s for %s',
                         'applied' if corr else 'cleared', doc_key)
            except Exception:
                stats['errors'] += 1
                log.exception('docbrowser: correction pass failed for %s', doc_key)

    # 4. Prune orphans: category-tree entries no longer backed by any KB file.
    for p in sorted(root.rglob('*')):
        try:
            rel = p.relative_to(root)
            if not _is_managed_top(rel):
                continue  # by-date/, rejected/, _reviews/, hidden — out of reach
            if p in desired:
                continue
            if p.is_dir() and not p.is_symlink():
                continue  # handled implicitly via children; rmdir below
            if not (p.is_file() or p.is_symlink()):
                continue
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode) or st.st_nlink > 1:
                p.unlink()
                stats['links_pruned'] += 1
            else:
                stats['links_refused'] += 1
                log.warning('docbrowser: refusing to prune %s (sole copy, nlink=1)', p)
        except Exception:
            stats['errors'] += 1
            log.exception('docbrowser: prune error at %s', p)

    # 5. Drop now-empty category dirs (never by-date/, rejected/, _reviews/).
    for p in sorted((x for x in root.rglob('*') if x.is_dir() and not x.is_symlink()),
                    key=lambda x: len(x.parts), reverse=True):
        try:
            rel = p.relative_to(root)
            if not _is_managed_top(rel):
                continue
            if any(p.iterdir()):
                continue
            p.rmdir()
        except Exception:
            pass

    if any(stats.values()):
        log.info('docbrowser sweep: %s', stats)
    return stats


async def _sweep_guarded() -> None:
    async with _lock():
        try:
            await _sweep_once()
        except Exception:
            log.exception('docbrowser: sweep crashed')


async def _start_loop() -> None:
    if _state['loop_task'] is not None and not _state['loop_task'].done():
        return
    _state['loop_task'] = asyncio.create_task(_loop(), name='docbrowser-sweep')
    log.info('docbrowser: sweep loop started (interval=%ss)', valves.sweep_interval_sec)


async def _loop() -> None:
    # Initial sweep shortly after startup, then on the interval.
    while True:
        try:
            await asyncio.sleep(min(valves.sweep_interval_sec, 10))
            await _sweep_guarded()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('docbrowser: sweep iteration failed')
            await asyncio.sleep(30)


def _schedule_hint() -> None:
    """Debounced early sweep in response to a KB-membership event."""
    if not valves.enable_events:
        return
    if _state['hint_task'] is not None and not _state['hint_task'].done():
        _state['hint_task'].cancel()

    async def _run():
        try:
            await asyncio.sleep(valves.event_debounce_sec)
            await _sweep_guarded()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception('docbrowser: hinted sweep failed')

    _state['hint_task'] = asyncio.create_task(_run(), name='docbrowser-hint')


# ── module state (fresh on every function load) ──────────────────────────────
_state = {
    'lock': None,          # asyncio.Lock guarding sweeps
    'loop_task': None,     # periodic sweep task
    'hint_task': None,     # pending debounced sweep
    'running': False,
}


def _lock() -> asyncio.Lock:
    if _state['lock'] is None:
        _state['lock'] = asyncio.Lock()
    return _state['lock']


class Event:
    async def event(self, event, __event_name__: str = '', **kwargs) -> None:
        """Event handler. Receives every event dispatched by OWUI; we act on
        startup (start the sweep loop) and KB-membership hints (early sweep).
        Exceptions here are swallowed by OWUI's best-effort dispatch, so the
        periodic loop remains the source of truth.

        NOTE: OWUI resolves the handler via getattr(Event_instance, 'event'),
        so this MUST be a method on the Event class, not a module-level
        function."""
        try:
            if __event_name__ == 'system.startup.completed':
                await _start_loop()
                return
            # Lazy start: if the plugin was installed after boot, the first
            # event of any kind kicks the loop off.
            if _state['loop_task'] is None or _state['loop_task'].done():
                await _start_loop()
            if __event_name__ in _HINT_EVENTS:
                _schedule_hint()
        except Exception:
            log.exception('docbrowser: event handler failed')
