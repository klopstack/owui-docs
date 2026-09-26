"""
title: Docstree
author: benklop
version: 1.0
description: Maintains the docs-set category link tree (<category>/<stem>/<name>)
  as a materialized view of the OWUI knowledge bases. Reference files
  (external_ref.path pointing at by-date masters) get a hardlink — or a
  relative symlink when hardlinks are impossible — into the category tree so
  the KB directory layout is browsable on the filesystem. A periodic sweep is
  the source of truth; knowledge.file.* / file.deleted events are debounced
  hints that trigger an early sweep. Safety: only ever creates or unlinks
  paths under <category>/<stem>/; by-date/ and rejected/ are never touched,
  and nlink==1 regular files are never modified (sole copies are sacred).
required_open_webui_version: 0.11.4
"""

import asyncio
import logging
import os
import stat
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger("docstree")

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

_NEVER_TOUCH_TOP_DIRS = frozenset({'by-date', 'rejected'})


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
        description='Periodic full-sweep interval in seconds',
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
            log.exception('docstree: event handler failed')


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


def _root() -> Path:
    return Path(valves.docs_set_root)


def _is_managed_top(rel: Path) -> bool:
    """True if rel is a category path we own (not by-date/, rejected/, hidden)."""
    if not rel.parts or '..' in rel.parts:
        return False
    top = rel.parts[0]
    return top not in _NEVER_TOUCH_TOP_DIRS and not top.startswith('.')


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


async def _collect_desired(root: Path) -> dict[Path, Path]:
    """Walk every KB's join table → {desired_link_path: by_date_master}.

    Truth = the knowledge_file join table (cheap, precise). Only reference
    files (external_ref.path) participate — byte-uploaded files live in OWUI
    storage, not docs-set, so they have no tree representation.
    """
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.knowledge import Knowledges

    desired: dict[Path, Path] = {}
    async with get_async_db_context() as db:
        kbs = await Knowledges.get_knowledge_bases(db=db)
        for kb in kbs:
            try:
                pairs = await Knowledges.get_files_with_directory_ids(kb.id, db=db)
            except Exception:
                log.exception('docstree: could not list files for KB %s', kb.id)
                continue
            for file, _dir_id in pairs:
                try:
                    umeta = _upload_meta(file)
                    source_path = umeta.get('source_path')
                    ext_ref = umeta.get('external_ref') or {}
                    master = ext_ref.get('path') if isinstance(ext_ref, dict) else None
                    if not source_path or not master:
                        continue
                    dst = _desired_link(root, source_path)
                    master_p = _master_under_root(root, master)
                    if dst is None or master_p is None:
                        continue
                    desired[dst] = master_p
                except Exception:
                    log.exception('docstree: bad metadata for file %s', getattr(file, 'id', '?'))
    return desired


async def _sweep_once() -> dict:
    """One full reconciliation pass. Idempotent; per-item errors are logged
    and skipped so one bad file never aborts the sweep."""
    root = _root()
    stats = {'created': 0, 'repaired': 0, 'pruned': 0, 'refused': 0, 'errors': 0}
    if not root.is_dir():
        log.warning('docstree: root %s missing — sweep skipped', root)
        return stats

    desired = await _collect_desired(root)

    # 1. Materialize missing / repair wrong links.
    for dst, master in desired.items():
        try:
            if not master.is_file():
                # Dangling master (removed out-of-band) — nothing to link.
                continue
            kind = _link_kind(dst, master)
            if kind == 'converged':
                continue
            if kind == 'refuse':
                # A sole-copy regular file occupies the slot — never destroy
                # it; the operator must resolve the collision by hand.
                stats['refused'] += 1
                log.warning('docstree: refusing to touch %s (sole copy)', dst)
                continue
            if kind in ('repair', 'missing'):
                if dst.is_symlink():
                    dst.unlink()
                    stats['repaired'] += 1
                elif dst.exists():
                    # Regular file (nlink>1, i.e. a hardlink to something
                    # else) squatting in the slot — os.link would raise
                    # FileExistsError, so refuse rather than clobber.
                    stats['refused'] += 1
                    log.warning('docstree: refusing to overwrite %s (regular file)', dst)
                    continue
                _link_or_symlink(master, dst)
                stats['created'] += 1
        except Exception:
            stats['errors'] += 1
            log.exception('docstree: failed to materialize %s', dst)

    # 2. Prune orphans: category-tree entries no longer backed by any KB file.
    for p in sorted(root.rglob('*')):
        try:
            rel = p.relative_to(root)
            if not _is_managed_top(rel):
                continue  # by-date/, rejected/, hidden — structurally out of reach
            if p in desired:
                continue
            if p.is_dir() and not p.is_symlink():
                continue  # handled implicitly via children; rmdir below
            if not (p.is_file() or p.is_symlink()):
                continue
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode) or st.st_nlink > 1:
                p.unlink()
                stats['pruned'] += 1
            else:
                stats['refused'] += 1
                log.warning('docstree: refusing to prune %s (sole copy, nlink=1)', p)
        except Exception:
            stats['errors'] += 1
            log.exception('docstree: prune error at %s', p)

    # 3. Drop now-empty category dirs (never by-date/ or rejected/).
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
        log.info('docstree sweep: %s', stats)
    return stats


async def _sweep_guarded() -> None:
    async with _lock():
        try:
            await _sweep_once()
        except Exception:
            log.exception('docstree: sweep crashed')


async def _start_loop() -> None:
    if _state['loop_task'] is not None and not _state['loop_task'].done():
        return
    _state['loop_task'] = asyncio.create_task(_loop(), name='docstree-sweep')
    log.info('docstree: sweep loop started (interval=%ss)', valves.sweep_interval_sec)


async def _loop() -> None:
    # Initial sweep shortly after startup, then on the interval.
    while True:
        try:
            await asyncio.sleep(min(valves.sweep_interval_sec, 10))
            await _sweep_guarded()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('docstree: sweep iteration failed')
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
            log.exception('docstree: hinted sweep failed')

    _state['hint_task'] = asyncio.create_task(_run(), name='docstree-hint')
