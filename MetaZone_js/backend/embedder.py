"""
embedder.py — Stage 5: real Meta Embedder pipeline, ported from
ui/embed_window.py's EmbedContent (CSV+folder -> EXIF write via
ExifTool). Same intent as session.py for Stage 4: the actual logic
(one-time file index build, per-row lookup, embed_metadata_one,
optional title-based rename) is preserved, calling the same
unmodified core.utils functions; only the UI-facing seam changes.

Convention preserved (see project knowledge doc, item 7): file
matching MUST use a one-time build_file_index/index_lookup, never
per-row find_file/find_recursive -- that was a confirmed root cause of
a multi-minute freeze on a 70-row batch. Ported unchanged.
"""
import csv
import os
import re
import threading
import time

from core.utils import (
    find_exiftool, build_file_index, index_lookup, embed_metadata_one,
    read_existing_titles_batch,
)
from core import stats_db

import bridge


# ── Embed "Word Count" filename setting (v0.9.7.5) ──────────────────────
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _truncate_title_words(title, word_count):
    """word_count is whatever the frontend sent as
    `replace_filename_words`: "5"/"8"/"12" (word counts, as
    strings -- an int also works) or "full"/None/anything else, which
    all mean "use the complete title" -- this is the documented
    default and matches the exact previous behavior for anyone who
    upgrades without touching the new setting. Splits on whitespace
    (not a fixed-width slice) so multiple/irregular spaces in the
    source title don't produce empty "words" or an off-by-one count."""
    if word_count in (None, "", "full", "Full Title"):
        return title
    try:
        n = int(word_count)
    except (TypeError, ValueError):
        return title
    if n <= 0:
        return title
    words = title.split()
    if n >= len(words):
        return title
    return " ".join(words[:n])


def _sanitize_filename_component(name):
    """Turns a title (or a word-count-truncated portion of one) into a
    safe filesystem filename component. Handles: the classic Windows-
    illegal character set plus raw control characters, straight AND
    curly/smart quotes, repeated internal whitespace, and leading/
    trailing whitespace or trailing periods (both of which Windows
    silently strips itself -- stripped here too so what's logged/
    shown matches what actually lands on disk, and it matters on
    macOS/Linux as well, where a trailing '.' is legal but confusing).
    Falls back to "untitled" if nothing usable is left, and caps
    length the same way the original _rename_to_title always did.
    This is the ONE place filename sanitization happens for the Embed
    rename path -- word-count truncation always flows through here
    rather than being written to disk un-sanitized."""
    name = name.replace("\u2018", "").replace("\u2019", "")  # ‘ ’
    name = name.replace("\u201c", "").replace("\u201d", "")  # “ ”
    name = _INVALID_FILENAME_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    while name and name[-1] in " .":
        name = name[:-1]
    return name[:180] or "untitled"


class EmbedSession:
    def __init__(self, event_prefix=""):
        self.csv_rows = []
        self.csv_headers = []
        self.folder = ""
        self._file_index = None
        self._file_index_key = None
        self.running = False
        self._rename_lock = threading.Lock()
        # Batch/Automation integration: lets a second, independent
        # EmbedSession (the Batch queue's) run without its embed_log/
        # embed_progress/embed_completed events colliding with the Meta
        # Embedder page's own events. Default "" preserves the exact
        # existing event names for every current caller -- the JPEG/PNG
        # embed pipeline itself is unchanged.
        self.event_prefix = event_prefix

    def load_csv(self, path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            self.csv_rows = list(reader)
            self.csv_headers = list(reader.fieldnames or [])
        guess_folder = os.path.dirname(path) if not self.folder else self.folder
        return {
            "ok": True, "rows": len(self.csv_rows), "headers": self.csv_headers,
            "guessed_columns": self._guess_columns(self.csv_headers),
            "guessed_folder": guess_folder,
        }

    def clear(self):
        """Embed page "Clear All": forget the loaded CSV and the selected
        folder. Has to happen here, not just in the page -- csv_rows /
        folder live on this session, so a frontend-only reset would leave
        the old CSV in memory (start_embed would still see it) and
        load_csv() would keep reusing the old folder as its "guessed"
        folder for the next CSV. Refused while a run is in progress: the
        run thread is reading these rows, and a Clear that appears to
        work while embedding carries on underneath would be a lie.
        """
        if self.running:
            return {"ok": False, "error": "Embedding is running — wait for it to finish before clearing."}
        self.csv_rows = []
        self.csv_headers = []
        self.folder = ""
        self._file_index = None
        self._file_index_key = None
        return {"ok": True}

    @staticmethod
    def _guess_columns(headers):
        # Same hint table as api_dialog/embed_window's _update_combos.
        hints = {"filename": ["filename", "file", "name", "image"],
                 "title": ["title"], "keywords": ["keyword", "tag", "kw"],
                 "description": ["desc", "caption", "description"]}
        out = {}
        for field, needles in hints.items():
            out[field] = next((h for n in needles for h in headers if n in h.lower()), "")
        return out

    def set_folder(self, folder):
        # v0.9.x (Part 20): explicitly (re)selecting a folder -- via
        # Browse, a CSV-guessed folder, or a dropped folder -- is the
        # one moment a stale cached index could actually be wrong (the
        # user may have just added/removed files and is re-pointing the
        # app at this folder specifically to pick that up, even if it's
        # the same path as before). The preview_match cache above only
        # avoids rescanning on column/toggle changes *within* a
        # selection; it must never survive an explicit (re-)selection,
        # so this always invalidates rather than only on a path change
        # -- the next preview_match simply pays one fresh scan, same
        # one-time cost as picking a folder for the first time.
        self._file_index = None
        self._file_index_key = None
        self.folder = folder

    def preview_match(self, folder, file_col, use_subfolders, use_ext_match):
        """Same one-time-index approach as the real embed run -- this
        is the match-preview shown before committing, ported from
        _update_match_preview.

        v0.9.x fix (Part 20): this used to call build_file_index() on
        every single call, unconditionally -- but preview_match fires
        live as the user types/toggles in the UI (filename column
        picker, extension-match toggle), neither of which changes what
        files are on disk. For a large folder that's a full rescan per
        keystroke -- the same shape of regression build_file_index was
        originally introduced to fix (see the project's per-row-os.walk
        history). Now reuses self._file_index the same way
        _embed_thread already does below, keyed on (folder,
        use_subfolders) -- only rebuilds when the folder or the
        subfolder-recursion setting actually changed, which are the
        only two inputs that affect what's actually on disk to scan.
        """
        if not folder or not self.csv_rows or not file_col:
            return {"ok": True, "matched": 0, "total": len(self.csv_rows)}
        if self._file_index is not None and self._file_index_key == (folder, use_subfolders):
            index = self._file_index
        else:
            index = build_file_index(folder, use_subfolders)
            self._file_index = index
            self._file_index_key = (folder, use_subfolders)
        matched = 0
        for row in self.csv_rows:
            fn = (row.get(file_col) or "").strip()
            if fn and index_lookup(index, fn, use_ext_match):
                matched += 1
        return {"ok": True, "matched": matched, "total": len(self.csv_rows)}

    def dry_run(self, folder, columns, options):
        """v0.9.4.1: categorizes every CSV row against the same
        one-time file index preview_match/the real run use, without
        modifying a single file on disk. Categories:
          - matched: filename present, a file was found for it, that
            file's extension is embeddable, and it has no existing
            Title tag.
          - already_embedded: same as matched, but the file already
            has a non-empty Title tag -- added in v0.9.4.1 (was
            explicitly flagged as unimplemented in v0.9.4). Turned out
            to be cheap enough to add for real: exiftool has always
            supported reading many files in a single invocation (-j,
            JSON output), so this is one batch subprocess call for
            every matched file, not one call per file -- see
            core.utils.read_existing_titles_batch. If that batch call
            fails outright (exiftool missing, unreadable, etc.), every
            otherwise-matched file just falls back to "matched" rather
            than being mis-labeled either way.
          - missing_image: filename present but no file found for it.
          - missing_metadata: the row's filename column itself is
            blank (nothing to even look up).
          - unsupported: a file WAS found, but its extension isn't one
            the app embeds metadata into.
        Still 100% non-destructive -- the added exiftool call is a
        read-only -Title query, not a write.
        """
        if not self.csv_rows:
            return {"ok": False, "error": "Load a CSV first."}
        if not folder:
            return {"ok": False, "error": "Select a folder."}
        col_f = columns.get("filename")
        if not col_f:
            return {"ok": False, "error": "Select the filename column."}

        use_sub = options.get("subfolders", True)
        use_ext = options.get("match_ext_only", True)
        if self._file_index is not None and self._file_index_key == (folder, use_sub):
            index = self._file_index
        else:
            index = build_file_index(folder, use_sub)
            self._file_index = index
            self._file_index_key = (folder, use_sub)

        from core.constants import ALL_SUPPORTED_EXTS
        rows_out = []
        counts = {"matched": 0, "missing_image": 0, "missing_metadata": 0, "unsupported": 0, "already_embedded": 0}

        # First pass: everything decidable from the file index alone
        # (no exiftool call needed yet) -- also collects the set of
        # matched filepaths so the existing-title check below is one
        # batch read for exactly the files that need it, not the
        # whole folder.
        prelim = []
        matched_paths = []
        for i, row in enumerate(self.csv_rows):
            fn = (row.get(col_f) or "").strip()
            fp = None
            if not fn:
                category = "missing_metadata"
            else:
                fp = index_lookup(index, fn, use_ext)
                if not fp:
                    category = "missing_image"
                else:
                    ext = os.path.splitext(fp)[1].lower()
                    category = "matched" if ext in ALL_SUPPORTED_EXTS else "unsupported"
                    if category == "matched":
                        matched_paths.append(fp)
            prelim.append({"row": i, "filename": fn, "filepath": fp, "category": category})

        existing_titles = {}
        if matched_paths:
            et = find_exiftool()
            if et:
                existing_titles = read_existing_titles_batch(et, matched_paths)

        for entry in prelim:
            if entry["category"] == "matched" and existing_titles.get(entry["filepath"]):
                entry["category"] = "already_embedded"
            counts[entry["category"]] += 1
            rows_out.append({"row": entry["row"], "filename": entry["filename"], "category": entry["category"]})

        return {"ok": True, "total": len(self.csv_rows), "counts": counts, "rows": rows_out}

    def start_embed(self, folder, columns, options):
        if self.running:
            return {"ok": False, "error": "Embed already running"}
        et = find_exiftool()
        if not et:
            return {"ok": False, "error": "exiftool not found. Place it next to the app."}
        if not self.csv_rows:
            return {"ok": False, "error": "Load a CSV first."}
        if not folder:
            return {"ok": False, "error": "Select a folder."}
        col_f = columns.get("filename")
        if not col_f:
            return {"ok": False, "error": "Select the filename column."}

        self.running = True
        threading.Thread(target=self._embed_thread, args=(et, folder, columns, options),
                          daemon=True).start()
        return {"ok": True, "total": len(self.csv_rows)}

    def _embed_thread(self, et, folder, columns, options):
        col_f = columns.get("filename")
        col_t = columns.get("title")
        col_k = columns.get("keywords")
        col_d = columns.get("description")
        use_sub = options.get("subfolders", True)
        use_ext = options.get("match_ext_only", True)
        rm_prog = options.get("remove_progressive", True)
        rm_copy = options.get("remove_copyright", True)
        replace_fn = options.get("replace_filename", False)
        # v0.9.7.5: how much of the Title to use when Replace Filename
        # is on -- "5"/"8"/"12"/"full", backend-owned (see
        # _truncate_title_words/_sanitize_filename_component above) so
        # Batch/automation or any other future caller of this same
        # _embed_thread can't accidentally bypass the word-count rule
        # by only updating the frontend. Defaults to "full" -- the
        # exact old behavior -- if the caller doesn't send it at all.
        replace_fn_words = options.get("replace_filename_words", "full")
        total = len(self.csv_rows)

        # One scan for the whole batch, reusing the preview's index if
        # it's still current for this exact (folder, subfolders) pair.
        if self._file_index is not None and self._file_index_key == (folder, use_sub):
            index = self._file_index
        else:
            index = build_file_index(folder, use_sub)
            self._file_index = index
            self._file_index_key = (folder, use_sub)

        bridge.emit(self.event_prefix + "embed_log", {"msg": f"Started — {total} rows"})
        counts = {"ok": 0, "skipped": 0, "errors": 0}
        lock = threading.Lock()
        done = [0]
        start_time = time.time()

        def process_row(row, i):
            fn = (row.get(col_f) or "").strip()
            if not fn:
                with lock:
                    counts["skipped"] += 1; done[0] += 1
                self._progress(counts, done[0], total)
                return
            fp = index_lookup(index, fn, use_ext)
            if not fp:
                with lock:
                    counts["skipped"] += 1; done[0] += 1
                bridge.emit(self.event_prefix + "embed_log", {"msg": f"Not found: {fn}", "level": "warn"})
                self._progress(counts, done[0], total)
                return
            title = (row.get(col_t) or "").strip() if col_t else ""
            kw_raw = (row.get(col_k) or "").strip() if col_k else ""
            desc = (row.get(col_d) or "").strip() if col_d else ""
            actual = os.path.basename(fp)
            ok, msg, final_path = embed_metadata_one(et, fp, title, kw_raw, desc, rm_prog, rm_copy)
            if ok:
                final_name = os.path.basename(final_path)
                if replace_fn and title:
                    new_path = self._rename_to_title(final_path, title, replace_fn_words)
                    if new_path:
                        final_name = os.path.basename(new_path)
                note = f"  ({msg.split('  (', 1)[1]}" if "  (" in msg else ""
                with lock:
                    counts["ok"] += 1; done[0] += 1
                bridge.emit(self.event_prefix + "embed_log", {"msg": f"{final_name}{note}", "level": "ok"})
            else:
                with lock:
                    counts["errors"] += 1; done[0] += 1
                bridge.emit(self.event_prefix + "embed_log", {"msg": f"{actual} — {msg}", "level": "error"})
            self._progress(counts, done[0], total)

        def _finish():
            summary = f"{counts['ok']} embedded · {counts['skipped']} not found · {counts['errors']} errors"
            bridge.emit(self.event_prefix + "embed_log", {"msg": f"Done — {summary}", "level": "done"})
            bridge.emit(self.event_prefix + "embed_completed", {"counts": counts, "total": total})
            self.running = False
            seconds = time.time() - start_time
            if counts["ok"] > 0:
                stats_db.record("embedding", "completed", count=counts["ok"], seconds=seconds,
                                 detail=f"Files: {counts['ok']}")
            if counts["errors"] > 0:
                stats_db.record("embedding", "failed", count=counts["errors"])

        # concurrency is now user-configurable (Embed page's new
        # "Concurrent" slider, same 1-20 range as Meta Generator's) --
        # previously hardcoded to 6. Falls back to the old hardcoded
        # value if the frontend doesn't send one (e.g. an older popup
        # window still cached in a running session).
        concurrency = int(options.get("concurrency") or 6)
        from workers.task_manager import TaskManager
        TaskManager().run_batch(self.csv_rows, process_row, max_workers=concurrency, on_all_done=_finish)

    def _progress(self, counts, done, total):
        bridge.emit(self.event_prefix + "embed_progress", {"done": done, "total": total, **counts})

    def _rename_to_title(self, fp, title, word_count="full"):
        """word_count: "5"/"8"/"12"/"full" (see _truncate_title_words) --
        the Title column is truncated to that many leading words
        (word_count="full", the default, keeps the complete title, the
        exact original behavior) and THEN run through the same
        sanitization the original single-arg version always did (see
        _sanitize_filename_component), so word-count truncation never
        bypasses illegal-character/whitespace handling. Collision-safe
        rename (the " (1)", " (2)"… suffix loop) and the rename lock
        that makes concurrent embeds race-safe are both unchanged."""
        portion = _truncate_title_words(title, word_count)
        base = _sanitize_filename_component(portion)
        ext = os.path.splitext(fp)[1]
        directory = os.path.dirname(fp)
        with self._rename_lock:
            new_path = os.path.join(directory, base + ext)
            n = 1
            while os.path.exists(new_path) and os.path.normcase(new_path) != os.path.normcase(fp):
                new_path = os.path.join(directory, f"{base} ({n}){ext}")
                n += 1
            try:
                os.rename(fp, new_path)
                return new_path
            except Exception:
                return None
