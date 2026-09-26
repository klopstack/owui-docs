# owui-docs

Open WebUI plugin: **consolidated document meta-entries** for the
[klopstack/llm-stack](https://github.com/klopstack/llm-stack) docs-pipeline
knowledge bases.

One physical document (duplex scan, single scan, photo, born-digital PDF,
soon: audio) produces several by-date artifacts. The pipeline pushes only the
markdown into the KB; this plugin makes the KB *look* like one entry per
document:

- **doc_meta adoption** — for every reference KB file (upload metadata
  carries `external_ref.path` → a by-date master), the sweep synthesizes a
  media-generic `doc_meta` block from the by-date siblings and OWUI
  reference file rows, and writes it into the KB file's
  `meta.data.doc_meta`. The OWUI fork's `Files.svelte` groups rows by
  `doc_meta.doc_key` and renders the extent column + download button from it.
- **Link tree** (absorbed from the former `docstree` plugin) — hardlinks
  (or relative symlinks) the by-date masters into the
  `<category>/<stem>/` tree, prunes orphans, never touches `by-date/`,
  `rejected/`, `_reviews/`, or nlink==1 sole copies.
- **Metadata guardian** — alerts (log) on reference files whose by-date
  master vanished.
- **Correction groundwork** — watches `<docs_set>/_reviews/<doc_key>/
  correction.json` and stamps `doc_meta.status="corrected"` (+
  `correction_sha`). Consuming corrections is a later plan.

## doc_meta contract

Frozen in [`docbrowser.py`](docbrowser.py) (`synthesize_doc_meta`); consumed
by the fork frontend. See the schema comment in the source. Highlights:

- `doc_key` = by-date basename stem (e.g. `20260925-143022`, including any
  `-<sha8>` same-second collision suffix).
- `primary` = the download/preview target (`layered` → `audio` → `orig`
  preference) with `file_id` (→ `GET /api/v1/files/{id}/content`, which the
  reference-files fork streams from `REFERENCE_FILES_ROOT`) and a
  media-generic `extent` (`{"unit": "pages", "value": N}` for PDFs; null
  until the audio lane lands).
- `artifacts[]` = every by-date sibling with its role, `file_id` (null when
  no OWUI row exists — e.g. scan originals), `relpath`, `media_type`.

## Deploy

Single-file plugin (like the old docstree): upload `docbrowser.py` via
`POST /api/v1/functions/create` (or the llm-stack installer script, which
generalizes to this repo). Valves: `docs_set_root` (default `/mnt/docs-set`),
`sweep_interval_sec`, `event_debounce_sec`, `enable_events`.

Requirements: Open WebUI ≥ 0.11.4 with the reference-files fork (so
`external_ref` uploads + `ref:` streaming exist) and `pypdf` (in the stock
image).

## Tests

```
python3 -m pytest tests/ -q
```

Pure-Python core tests; no OWUI install needed (the plugin lazy-imports OWUI
models inside the sweep).
