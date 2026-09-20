"""
bridge.py — the seam between the existing Python backend and the new
JS frontend. This is Stage 3 of the migration: prove the round trip
using REAL existing modules (core/config.py, engine/ai_providers.py),
not mocked data.

Two directions of communication:

1. JS -> Python (request/response): methods on `Api`, called from JS as
   `pywebview.api.<method>(...)`. pywebview marshals these automatically.

2. Python -> JS (push events, e.g. task_progress): background threads
   put (event_name, payload) tuples on `ui_event_queue`, exactly like
   main_window.py's `_ui_action_queue`. A single drain loop (started
   once the window is loaded) pulls from that queue and calls
   `window.evaluate_js(...)` to fire a JS-side event dispatcher.

   This preserves the existing thread-safety rule from the project
   knowledge doc ("no Tk call ever originates on a background thread")
   in its new form: no evaluate_js call ever originates on a
   background thread either. Worker threads only ever touch the queue.
"""
import json
import os
import queue
import threading
import time
import uuid

import webview

from core import config as core_config
from engine import ai_providers
from session import Session
from embedder import EmbedSession
import settings as settings_mod
from prompt2prompt import PromptToPromptSession
import dashboard as dashboard_mod
from automation.queue_manager import AutomationQueue
from automation.job_controller import AutomationController

# Same role as main_window.py's self._ui_action_queue, just retargeted
# to JS events instead of Tk widget mutations.
ui_event_queue = queue.Queue()

# Set by app.py's main() once the Api is created, so Python-side event
# handlers bound outside the js_api bridge (e.g. real drag-drop, see
# app.py's _bind_real_drag_drop) can reach the same session state.
api_instance = None

# Set by app.py's main() -- used by Api.open_api_manager_popup() to
# spawn a real secondary pywebview window (not a same-window modal),
# matching the person's explicit request for a real small popup
# window, same interface, smaller form factor.
main_window = None
frontend_dir = None

# Throttle: coalesce rapid-fire progress events instead of pushing every
# single one across the JS bridge (the spec explicitly calls this out
# — "don't send 0.01%, 0.02%, 0.03%..."). 60ms matches roughly one
# frame at ~16fps, well under human-perceptible lag, while collapsing
# bursts from fast batches.
_DRAIN_INTERVAL_SEC = 0.06

# v0.9.x (Part 17): caps how many queued events go into a single
# evaluate_js call (see start_event_drain below) -- see the comment at
# that call site for why.
_MAX_EVENTS_PER_CALL = 40

# v0.9.6 ROOT-CAUSE FIX (perf item 4): the count-based cap above still
# let several large payloads through in one evaluate_js call -- 40
# thumb_ready events, even after the session.py fix that bounds each
# individual thumbnail to a small 160x160 JPEG, can still add up to a
# multi-hundred-KB JS string literal, which is exactly the "several
# large thumb_ready events can still overload the WebView/JS side"
# bug the report calls out by name. This byte cap makes chunking
# payload-size-aware in addition to count-based: whichever limit is
# hit first ends the current chunk.
_MAX_BATCH_BYTES = 120_000

# v0.9.6.5 ROOT-CAUSE FIX: the old loop drained the ENTIRE queue into
# `batch` and then looped `while idx < n: _send_chunk(...)` until every
# chunk was sent, with no cap -- so a big vector/video import burst
# (dozens of 120KB-ish thumb_ready chunks queued up) got hammered
# through evaluate_js back-to-back, tick after tick, with nothing but
# a single time.sleep(_DRAIN_INTERVAL_SEC) at the very end of the
# whole burst. evaluate_js is a synchronous round-trip into the
# WebView's JS engine; sending many of them with no gap between them
# is exactly what starves the WebView's own input/paint message pump,
# which is what actually caused "scrolling/clicking/dragging becomes
# unresponsive" during a big import even after payloads were bounded.
#
# This caps how many chunks go out per drain tick -- any leftover is
# requeued (via _requeue_front) and picked up on a LATER tick, each
# tick separated by the normal _DRAIN_INTERVAL_SEC sleep PLUS a short
# yield between chunks within the same tick. A large backlog now
# drains gradually over many ticks instead of in one uninterrupted
# burst -- vector/video processing can still take real time (that's
# expected and fine), but the WebView gets regular, guaranteed gaps to
# process a scroll/click/drag in between, matching the report's actual
# acceptance criterion ("vector/video processing may take time; the
# UI must not become heavy").
_MAX_CHUNKS_PER_TICK = 2

# Short yield between consecutive evaluate_js calls within the same
# tick (only relevant when _MAX_CHUNKS_PER_TICK > 1) -- gives the
# WebView's message loop a slice of time to service a pending input
# event between two backend-event deliveries, instead of only ever
# getting a gap once per full _DRAIN_INTERVAL_SEC tick.
_INTER_CHUNK_YIELD_SEC = 0.02

# v0.9.6.5: event kinds where only the LATEST value for a given key
# matters -- an older queued copy is genuinely obsolete the moment a
# newer one for the same key exists, so it's dropped before ever
# reaching evaluate_js rather than wastefully sent and immediately
# overwritten client-side. Keyed by (event_name, path) for per-file
# events (thumb_ready/file_meta_ready/p2p_image_thumb -- a file's 2nd
# thumbnail always supersedes its 1st), or by event_name alone for
# single-slot UI indicators (status text, progress bars, the undo
# count) where there's only ever "the current one".
#
# Deliberately NOT included: card_update, card_removed, cards_removed,
# card_restored, task_completed, and everything else -- those each
# carry state that would be genuinely LOST (not just superseded) if
# collapsed, e.g. two different paths' card_update events, or two
# separate cards_removed batches naming different paths. Coalescing
# is only safe where "keep just the newest" is truly lossless.
_COALESCE_BY_PATH_SUFFIXES = ("thumb_ready", "file_meta_ready", "p2p_image_thumb")
_COALESCE_LATEST_SUFFIXES = ("status_text", "task_progress", "p2p_progress", "undo_state")

# v0.9.6.5 (Clear/Stop verification): exact event base-names that
# become unconditionally stale the moment Session.clear() runs (every
# path they could reference has just been wiped from the session) --
# see purge_stale_events() below, called from Session.clear().
_STALE_ON_CLEAR_SUFFIXES = ("thumb_ready", "file_meta_ready", "card_update", "task_progress", "status_text")


def _coalesce_key(event_name, payload):
    """Returns a hashable key if this event is safe to coalesce (only
    the latest matters), or None if it must be kept as-is. See the
    _COALESCE_*_SUFFIXES comment above for which event kinds qualify
    and why the rest don't."""
    for suffix in _COALESCE_BY_PATH_SUFFIXES:
        if event_name.endswith(suffix):
            path = payload.get("path") if isinstance(payload, dict) else None
            if path is not None:
                return (event_name, path)
    for suffix in _COALESCE_LATEST_SUFFIXES:
        if event_name.endswith(suffix):
            return (event_name, None)
    return None


def _coalesce(batch):
    """Drops every entry in `batch` that's superseded by a LATER entry
    with the same coalesce key, keeping relative order of everything
    else unchanged. Two-pass, O(n): first find each key's last index,
    then keep only non-coalescable entries and each key's last
    occurrence. This is what satisfies "never queue multiple outdated
    thumbnail updates for the same file" -- the outdated ones are
    removed here, before a single byte of them ever reaches
    evaluate_js, not merely raced against a newer one client-side.

    Items are (event_name, payload) OR (event_name, payload,
    enqueued_at) tuples -- this only ever looks at the first two
    positions, so it works unchanged for either shape."""
    last_index = {}
    for i, item in enumerate(batch):
        key = _coalesce_key(item[0], item[1])
        if key is not None:
            last_index[key] = i
    out = []
    for i, item in enumerate(batch):
        key = _coalesce_key(item[0], item[1])
        if key is not None and last_index[key] != i:
            continue  # a later update for this same key exists -- drop this stale one
        out.append(item)
    return out


def purge_stale_events(prefix):
    """v0.9.6.5 (Clear/Stop verification): called by Session.clear()
    to strip any already-queued-but-not-yet-drained event for THIS
    session's own event-namespace (built from `prefix`, e.g. "" for
    Meta Generator or "prompt_" for Image-to-Prompt) that would
    otherwise still reach evaluate_js after Clear already wiped the
    session's state -- e.g. a thumb_ready queued a moment before Clear
    for a path that no longer exists in all_paths. Bumping
    import_epoch/gen_epoch (already done by clear()) stops NEW stale
    events from being emitted going forward; this handles the ones
    that were already emitted and sitting in ui_event_queue before
    that bump landed.

    Only removes exact `prefix + suffix` matches for
    _STALE_ON_CLEAR_SUFFIXES -- deliberately NOT a prefix.startswith()
    match, since Meta Generator's prefix is "" and a startswith("")
    would match every event in the queue, including unrelated modules
    (P2P/embedder/automation) that call bridge.emit() directly with
    their own unprefixed names. card_removed/cards_removed/
    card_restored/undo_state are deliberately left alone -- delivering
    one after Clear is a harmless no-op (the path is already gone from
    cardEls either way), not a "repopulates the UI" risk, so purging
    them isn't necessary and would just be extra queue-rebuilding work
    for no behavioral benefit."""
    exact_names = {prefix + suffix for suffix in _STALE_ON_CLEAR_SUFFIXES}
    kept = []
    removed = 0
    try:
        while True:
            item = ui_event_queue.get_nowait()
            if item[0] in exact_names:
                removed += 1
                continue
            kept.append(item)
    except queue.Empty:
        pass
    for item in kept:
        ui_event_queue.put(item)
    return removed


def _estimate_bytes(event_name, payload):
    """Cheap per-event byte estimate for chunking -- doesn't need to
    be exact (it's a chunking heuristic, not a wire-size guarantee),
    just proportional to what json.dumps would actually produce. The
    dominant cost in this app's events is always a base64 image
    string (thumb_ready/p2p_image_thumb/card_restored), so summing
    those directly is both fast (no full json.dumps per event) and
    accurate where it actually matters.
    """
    total = len(event_name) + 16
    thumb = payload.get("thumb") if isinstance(payload, dict) else None
    if thumb:
        total += len(thumb)
    for key in ("result", "meta"):
        val = payload.get(key) if isinstance(payload, dict) else None
        if val:
            total += len(str(val))
    return total


def emit(event_name: str, payload: dict):
    """Call this from ANY background thread/worker instead of touching
    the window directly. Mirrors main_window.py's
    `self._ui_action_queue.put(...)` call sites.

    v0.9.6.5: stores a monotonic enqueue timestamp alongside the event
    -- purely a backend-side diagnostic (see _send_chunk's
    queue_wait logging), stripped before anything is ever sent to the
    frontend. This is the "backend event/bridge time" leg of the
    perf-diagnostic split the report asks for, distinct from
    session.py's existing preprocess/ai_call timing: how long an event
    sits in ui_event_queue before it's actually delivered via
    evaluate_js, which is exactly the thing _MAX_CHUNKS_PER_TICK
    throttling below trades off against raw delivery speed."""
    ui_event_queue.put((event_name, payload, time.monotonic()))


def start_event_drain(window):
    """Start exactly once, after the window has loaded. Replaces the
    five self.after() poll loops in main_window.py with one loop."""

    def _loop():
        while True:
            batch = []
            try:
                while True:
                    batch.append(ui_event_queue.get_nowait())
            except queue.Empty:
                pass
            if batch:
                # v0.9.6.5: coalesce BEFORE chunking -- a burst of
                # superseded thumb_ready/status/progress events never
                # even gets bundled into a chunk, let alone sent.
                batch = _coalesce(batch)
            if batch:
                # v0.9.x (Part 17) / v0.9.6 (perf item 4) / v0.9.6.5:
                # bounded both by count (_MAX_EVENTS_PER_CALL) and bytes
                # (_MAX_BATCH_BYTES) per evaluate_js call, AND now capped
                # to _MAX_CHUNKS_PER_TICK sent chunks per drain tick --
                # see _MAX_CHUNKS_PER_TICK's module-level comment for the
                # root cause this last part fixes (many bounded chunks
                # still hammered back-to-back with no gap was itself the
                # remaining "UI becomes heavy" cause).
                idx = 0
                n = len(batch)
                chunks_sent_this_tick = 0
                while idx < n:
                    if chunks_sent_this_tick >= _MAX_CHUNKS_PER_TICK:
                        _requeue_front(batch[idx:])
                        break
                    chunk, chunk_bytes = [], 0
                    while idx < n and len(chunk) < _MAX_EVENTS_PER_CALL:
                        event_name, payload = batch[idx][0], batch[idx][1]
                        size = _estimate_bytes(event_name, payload)
                        if chunk and chunk_bytes + size > _MAX_BATCH_BYTES:
                            break
                        chunk.append(batch[idx])
                        chunk_bytes += size
                        idx += 1
                    if not _send_chunk(window, chunk):
                        _requeue_front(batch[idx - len(chunk):])
                        break
                    chunks_sent_this_tick += 1
                    if idx < n and chunks_sent_this_tick < _MAX_CHUNKS_PER_TICK:
                        # v0.9.6.5: yield to the WebView's own message
                        # loop between two evaluate_js calls in the same
                        # tick, instead of firing them back-to-back --
                        # this is the actual gap that lets a pending
                        # scroll/click/drag get serviced mid-burst rather
                        # than only between whole drain ticks.
                        time.sleep(_INTER_CHUNK_YIELD_SEC)
            time.sleep(_DRAIN_INTERVAL_SEC)

    threading.Thread(target=_loop, daemon=True).start()


def _send_chunk(window, chunk):
    """Sends one already-bounded (count + bytes) list of events as a
    single evaluate_js call. Returns True on success, False if the
    call itself failed (caller re-queues and retries next tick).

    v0.9.6.5: `chunk` items may carry a 3rd element (enqueued_at, from
    emit()) -- stripped here before JSON-encoding (the frontend only
    ever expects [name, payload] pairs, see events.js's dispatch()),
    but used first to log queue-wait latency: the "backend event/
    bridge time" leg of this batch's perf-diagnostic requirement,
    printed the same way session.py's preprocess=/ai_call= lines are.
    Only logged when it's actually large enough to matter (throttling
    intentionally introduces *some* queueing -- that's the tradeoff
    for not hammering evaluate_js -- so a routine sub-100ms wait isn't
    noise worth printing every tick)."""
    now = time.monotonic()
    waits = [now - item[2] for item in chunk if len(item) > 2]
    if waits and max(waits) > 0.25:
        print(f"[MetaZone perf] bridge: sent {len(chunk)} events, "
              f"queue_wait max={max(waits):.2f}s avg={sum(waits)/len(waits):.2f}s", flush=True)
    wire_chunk = [[item[0], item[1]] for item in chunk]
    payload_json = json.dumps(wire_chunk)
    try:
        window.evaluate_js(f"window.__onBackendEvents({payload_json})")
        return True
    except Exception:
        # Real race found by running this under Xvfb: the page's own
        # <script> tags can still be loading when this loop's first
        # tick fires (external scripts load async even though
        # on_loaded already fired), so window.__onBackendEvents may
        # not exist yet. Caller re-queues (in original order, at the
        # front) instead of dropping it or crashing the drain thread
        # -- the next tick retries once the page has finished setting
        # up its event listener.
        return False


def _requeue_front(batch):
    """Put a whole batch back at the front of ui_event_queue, in its
    original order, for a retry on the next drain tick."""
    # queue.Queue has no native "put back in front" -- rebuild it.
    leftover = []
    try:
        while True:
            leftover.append(ui_event_queue.get_nowait())
    except queue.Empty:
        pass
    for item in batch + leftover:
        ui_event_queue.put(item)


class Api:
    """Exposed to JS as `pywebview.api.*`. Every method here should be
    a thin wrapper around an existing backend function -- no new
    business logic lives in this file."""

    def __init__(self):
        self.session = Session()
        # v0.8.5: the new Image to Prompt Generator page is a second,
        # fully independent batch -- its own imported files, its own
        # results/progress/running state -- reusing the exact same
        # ported Session/session.py pipeline Meta Generator already
        # uses (mode="prompt" instead of mode="meta"), just as a
        # separate instance so the two pages never share a batch.
        # event_prefix="prompt_" keeps its push-events (card_update,
        # task_progress, etc) from colliding with Meta Generator's.
        self.prompt_session = Session(event_prefix="prompt_")
        self.embed_session = EmbedSession()
        self.p2p_session = PromptToPromptSession()
        # Batch (formerly "Automation Queue -- BETA"): deliberately its
        # own object, never touching self.session -- see
        # automation/queue_manager.py. Lives inside the main window as
        # a normal nav page now, not a separate popup window.
        self.automation_queue = AutomationQueue()
        self.automation_controller = AutomationController(self.automation_queue)
        self._window = None  # set by app.py after window creation, needed for native dialogs

    # ---- App version (v0.9.3): the topbar version pill used to be a
    # second hardcoded string in index.html that silently drifted from
    # APP_VERSION (it still said "v0.9.1" while constants.py already
    # said v0.9.2) -- this is the single source of truth going forward. ----
    def get_app_version(self):
        from core.constants import APP_VERSION
        return {"ok": True, "version": APP_VERSION}

    def set_window(self, window):
        self._window = window

    # ---- Settings / config (real core/config.py, unmodified) ----
    def get_prefs(self):
        prefs = core_config.load_prefs()
        return {"ok": True, "prefs": prefs}

    def save_prefs(self, prefs):
        """Merge into the existing prefs, don't overwrite wholesale.
        core_config.save_prefs() writes exactly the dict it's given --
        calling it with a partial dict (e.g. just theme fields) would
        silently wipe out everything else, including stored API keys.
        Real bug caught before shipping, not a hypothetical."""
        current = core_config.load_prefs()
        current.update(prefs)
        core_config.save_prefs(current)
        return {"ok": True}

    # ---- Provider status (real engine/ai_providers.py, unmodified) ----
    def get_active_keys_summary(self):
        prefs = core_config.load_prefs()
        seq = ai_providers.get_active_keys(prefs)
        # stored_count/provider_count added for the control panel's new
        # counts-only summary (API Manager button removed from there --
        # it's still reachable from the sidebar and Dashboard). Reuses
        # the same real settings.get_provider_summary() the API Manager
        # page itself is built from, so these can't drift out of sync.
        provider_summary = settings_mod.get_provider_summary()
        stored_count = sum(len(p["keys"]) for p in provider_summary)
        provider_count = sum(1 for p in provider_summary if p["keys"])
        return {
            "ok": True,
            "active_count": len(seq),
            "providers": sorted({p for p, _key, _m, _i in seq}),
            "stored_count": stored_count,
            "provider_count": provider_count,
        }

    # ---- File import (Stage 4: real native dialog + real validation) ----
    def browse_images(self):
        """Native OS file picker -- returns real filesystem paths,
        exactly like ui/main_window.py's filedialog.askopenfilenames,
        just invoked through pywebview instead of tkinter.filedialog."""
        if self._window is None:
            return {"ok": False, "error": "window not ready"}
        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=(
                "Supported files (*.jpg;*.jpeg;*.png;*.webp;*.gif;*.tiff;*.tif;*.svg;*.eps;*.ai;*.mp4;*.mov)",
                "All files (*.*)",
            ),
        )
        if not paths:
            return {"ok": True, "accepted": [], "rejected": []}
        result = self.session.add_paths(list(paths))
        return {"ok": True, **result}

    def get_thumb(self, path):
        """Cached lookup for a path already prefetched by add_paths;
        returns immediately from memory rather than re-decoding."""
        b64 = self.session.thumb_cache.get(path)
        return {"ok": True, "thumb": b64}

    def clear_batch(self):
        self.session.clear()
        return {"ok": True}

    def get_batch_state(self):
        """So the frontend can rebuild its grid after a reload/reconnect
        without re-running generation -- mirrors what _render_page reads
        from self._all_paths / self._results / self._completion_order."""
        s = self.session
        cards = [{"path": p, "result": s.results.get(p, {})} for p in s.completion_order]
        return {"ok": True, "total": len(s.all_paths), "cards": cards,
                "running": s.running}

    def update_card_field(self, path, field, value):
        return self.session.update_field(path, field, value)

    # ---- Per-card actions (v0.8.9): Regenerate / Delete buttons now
    # under each card's thumbnail on the Meta Generator grid ----
    def delete_card(self, path):
        return self.session.delete_card(path)

    def delete_cards(self, paths):
        return self.session.delete_cards(paths)

    def restore_card(self, path, result):
        # v0.9.6: kept as a thin back-compat shim (unused by the new
        # toolbar Undo flow, which calls undo_last_delete() below) in
        # case anything else still calls it -- delegates straight
        # through in case old snapshot-shaped calls exist.
        return {"ok": False, "error": "deprecated -- use undo_last_delete()"}

    def undo_last_delete(self):
        """v0.9.6: backs the new persistent toolbar Undo button.
        Always undoes the OLDEST not-yet-undone deletion (FIFO), and
        the actual restored card data comes back to the frontend via
        the 'card_restored' event, not this call's return value --
        same request/emit split every other per-card action here uses.
        """
        return self.session.undo_next()

    def regenerate_card(self, path, mode, options):
        prefs = core_config.load_prefs()
        return self.session.regenerate_one(path, mode, options, prefs)

    # ---- Generation (Stage 4: real pipeline in session.py) ----
    def start_generation(self, mode, options):
        prefs = core_config.load_prefs()
        return self.session.start_generation(mode, options, prefs)

    def start_generation_for_paths(self, paths, mode, options):
        prefs = core_config.load_prefs()
        return self.session.start_generation_for_paths(paths, mode, options, prefs)

    # ---- Meta Generator platform/file-type options (real constants) ----
    def get_meta_options(self):
        from core.constants import PLATFORM_RULES, CONTENT_SUFFIXES
        return {"ok": True, "platforms": PLATFORM_RULES, "content_types": CONTENT_SUFFIXES}

    # ---- Image to Prompt Generator (v0.8.6): thin wrappers around
    # self.prompt_session mirroring self.session's methods 1:1, per
    # the handoff note -- no new generation logic, this page reuses
    # the exact same Session/session.py pipeline with mode="prompt". ----
    def get_prompt_options(self):
        from core.constants import PROMPT_GEN_STYLES
        return {"ok": True, "styles": PROMPT_GEN_STYLES}

    def browse_prompt_images(self):
        if self._window is None:
            return {"ok": False, "error": "window not ready"}
        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=(
                "Supported files (*.jpg;*.jpeg;*.png;*.webp;*.gif;*.tiff;*.tif;*.svg;*.eps;*.mp4;*.mov)",
                "All files (*.*)",
            ),
        )
        if not paths:
            return {"ok": True, "accepted": [], "rejected": []}
        result = self.prompt_session.add_paths(list(paths))
        return {"ok": True, **result}

    def get_prompt_thumb(self, path):
        b64 = self.prompt_session.thumb_cache.get(path)
        return {"ok": True, "thumb": b64}

    def clear_prompt_batch(self):
        self.prompt_session.clear()
        return {"ok": True}

    def get_prompt_batch_state(self):
        s = self.prompt_session
        cards = [{"path": p, "result": s.results.get(p, {})} for p in s.completion_order]
        return {"ok": True, "total": len(s.all_paths), "cards": cards,
                "running": s.running}

    def update_prompt_card_field(self, path, field, value):
        return self.prompt_session.update_field(path, field, value)

    # ---- Per-card actions (v0.8.9), mirroring Meta Generator's
    # delete_card/regenerate_card 1:1 against self.prompt_session ----
    def delete_prompt_card(self, path):
        return self.prompt_session.delete_card(path)

    def regenerate_prompt_card(self, path, options):
        prefs = core_config.load_prefs()
        return self.prompt_session.regenerate_one(path, "prompt", options, prefs)

    def start_prompt_generation(self, options):
        prefs = core_config.load_prefs()
        return self.prompt_session.start_generation("prompt", options, prefs)

    def pause_prompt_generation(self):
        return self.prompt_session.pause()

    def stop_prompt_generation(self):
        return self.prompt_session.stop()

    def export_prompt_csv(self, auto=False):
        return self._export_csv_session(self.prompt_session, auto)

    # ---- P2P results export (v0.8.7: Export TXT / Export CSV buttons
    # in the Generated Prompts panel -- a plain file save, not routed
    # through Session's CSV export since P2P has no per-file/path rows,
    # just a flat list of generated prompt strings) ----
    def export_p2p_prompts(self, prompts, fmt):
        if self._window is None:
            return {"ok": False, "error": "window not ready"}
        if not prompts:
            return {"ok": False, "error": "Nothing to export yet."}
        ext = "csv" if fmt == "csv" else "txt"
        path = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=f"p2p_prompts.{ext}",
            file_types=(f"{ext.upper()} files (*.{ext})", "All files (*.*)"),
        )
        if not path:
            return {"ok": False, "cancelled": True}
        path = path if isinstance(path, str) else path[0]
        try:
            if fmt == "csv":
                import csv
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow(["prompt"])
                    for p in prompts:
                        w.writerow([p])
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n\n".join(prompts))
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def pause_generation(self):
        return self.session.pause()

    def stop_generation(self):
        return self.session.stop()

    # ---- CSV export (v0.8.6: manual "Download CSV" button in Meta
    # Generator's control panel; auto-download already wired inside
    # session.py's _on_all_done via options.auto_download_csv, this is
    # only the manual path) ----
    def _export_csv_session(self, session, auto):
        """Shared by Meta Generator (self.session) and, once built, the
        Image to Prompt Generator (self.prompt_session) -- auto=True
        writes straight into the batch's common image folder using
        session.export_csv()'s existing #FolderName.csv naming with no
        dialog; auto=False opens a native Save dialog defaulting to that
        same folder/filename so the user can redirect it."""
        if auto:
            return session.export_csv()
        if self._window is None:
            return {"ok": False, "error": "window not ready"}
        folder = session._common_folder() or None
        default_name = os.path.basename(session._auto_csv_path(folder or "."))
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=folder or "",
            save_filename=default_name,
            file_types=("CSV files (*.csv)", "All files (*.*)"),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        dest = result[0] if isinstance(result, (list, tuple)) else result
        return session.export_csv(dest)

    def export_csv(self, auto=False):
        return self._export_csv_session(self.session, auto)

    # ---- Meta Embedder (Stage 5: real embedder.py pipeline) ----
    def browse_csv(self):
        if self._window is None:
            return {"ok": False, "error": "window not ready"}
        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("CSV files (*.csv)", "All files (*.*)"),
        )
        if not paths:
            return {"ok": False, "cancelled": True}
        res = self.embed_session.load_csv(paths[0])
        # v0.8.8: mirror load_csv_dropped's behavior -- if no folder has
        # been picked yet, adopt the CSV's own directory as the working
        # folder right away (not just as a display hint returned to the
        # frontend). This is what makes browse_embed_folder() below able
        # to open its dialog already pointed at that directory instead
        # of the OS's last-used/default location.
        if res.get("ok") and not self.embed_session.folder and res.get("guessed_folder"):
            self.embed_session.set_folder(res["guessed_folder"])
            res["folder"] = res["guessed_folder"]
        return res

    def browse_embed_folder(self):
        if self._window is None:
            return {"ok": False, "error": "window not ready"}
        # v0.8.8: start the native folder picker at whatever folder is
        # already known (set by a prior Browse, drag-drop, or -- most
        # commonly -- auto-guessed from the CSV's own location right
        # after loading it) instead of always opening at the OS default.
        start_dir = self.embed_session.folder or ""
        paths = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=start_dir)
        if not paths:
            return {"ok": False, "cancelled": True}
        folder = paths[0]
        self.embed_session.set_folder(folder)
        return {"ok": True, "folder": folder}

    def preview_embed_match(self, folder, file_col, use_subfolders, use_ext_match):
        return self.embed_session.preview_match(folder, file_col, use_subfolders, use_ext_match)

    def dry_run_embed(self, folder, columns, options):
        return self.embed_session.dry_run(folder, columns, options)

    def load_csv_dropped(self, path):
        """CSV drag-and-drop (v0.8.7): same load path as the Browse
        button, called from the element-scoped drop handler in app.py.
        load_csv() already auto-fills the folder guess (CSV's own
        directory) whenever no folder is set yet, so a dropped CSV with
        no prior folder selection grabs its own location for free."""
        res = self.embed_session.load_csv(path)
        if res.get("ok") and not self.embed_session.folder and res.get("guessed_folder"):
            self.embed_session.set_folder(res["guessed_folder"])
            res["folder"] = res["guessed_folder"]
        return res

    def set_embed_folder_dropped(self, path):
        """Folder (or file-within-folder) drag-and-drop onto the File
        Location step. Accepts either a real folder path or a file path
        (uses its parent directory) since OS file managers can hand
        over either depending on what got dragged."""
        if not path:
            return {"ok": False, "error": "no path"}
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if not folder or not os.path.isdir(folder):
            return {"ok": False, "error": "Not a valid folder"}
        self.embed_session.set_folder(folder)
        return {"ok": True, "folder": folder}

    def start_embed(self, folder, columns, options):
        return self.embed_session.start_embed(folder, columns, options)

    def clear_embed(self):
        # v0.9.8: Embed page "Clear All" -- see EmbedSession.clear().
        return self.embed_session.clear()

    # ---- Settings / API Manager (Stage 5: real settings.py, real
    # engine/ai_providers.validate_key) ----
    def get_provider_summary(self):
        return {"ok": True, "providers": settings_mod.get_provider_summary()}

    def add_api_key(self, provider, key):
        return settings_mod.add_key(provider, key)

    def set_key_active(self, provider, index, active):
        return settings_mod.set_key_active(provider, index, active)

    def set_all_keys_active(self, provider, active):
        return settings_mod.set_all_active(provider, active)

    def set_provider_model(self, provider, model_id):
        return settings_mod.set_model(provider, model_id)

    def delete_api_key(self, provider, index):
        return settings_mod.delete_key(provider, index)

    # ---- API Manager as a real popup window (not a same-window modal) ----
    def open_api_manager_popup(self):
        if frontend_dir is None:
            return {"ok": False, "error": "not ready"}
        popup = webview.create_window(
            "API Manager",
            url=os.path.join(frontend_dir, "index.html") + "?popup=api",
            js_api=self,
            width=480,
            height=720,
            min_size=(420, 500),
        )
        # The popup shares this same Api instance (and therefore the
        # same Session, EmbedSession, etc.) -- it's a smaller *view* of
        # the same app state, not a second app.
        threading.Thread(target=start_event_drain, args=(popup,), daemon=True).start()
        return {"ok": True}

    def validate_key_live(self, provider, key):
        # v0.9.x fix (Part 21): this used to call ai_providers.validate_key
        # (a synchronous urllib request, up to 12s timeout per provider,
        # see engine/ai_providers.py) directly on the same call that
        # pywebview dispatches every js_api method through. Any other
        # bridge call made while a validation is in flight -- Stop,
        # Generate, a card edit, anything -- would queue up behind it.
        # Now returns immediately with a request_id and does the actual
        # network call on a background thread, emitting the real result
        # as a "key_validated" event (same emit()/event-bridge pattern
        # used everywhere else in this app) once it's done. Frontend
        # side: see requestValidateKey() in settings.js, which wraps
        # this back into the same await-style call sites had before --
        # no visible behavior change, just no longer blocking.
        rid = str(uuid.uuid4())

        def _run():
            ok, msg = ai_providers.validate_key(provider, key)
            settings_mod.record_key_test(provider, key, ok, msg)
            emit("key_validated", {"request_id": rid, "ok": ok, "message": msg, "provider": provider, "key": key})

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "request_id": rid}

    def set_key_nickname(self, provider, index, nickname):
        return settings_mod.set_key_nickname(provider, index, nickname)

    def set_model_enabled(self, provider, model_id, enabled):
        return settings_mod.set_model_enabled(provider, model_id, enabled)

    def activate_valid_keys(self, provider):
        return settings_mod.activate_valid_keys(provider)

    def test_all_keys(self, provider):
        """v0.9.4 (item 15): tests every stored key for one provider,
        one at a time on a background thread (validate_key is a real
        blocking network call per key -- running them in parallel would
        just hammer the provider's API with N simultaneous requests for
        no real benefit here, unlike generation's failover concurrency
        which is deliberately parallel across *different* work). Each
        result is persisted and emitted the same way a single manual
        Test click already is, so the UI updates key-by-key as they
        finish rather than waiting for the whole batch."""
        summary = next((p for p in settings_mod.get_provider_summary() if p["provider"] == provider), None)
        keys = [k["key"] for k in summary["keys"]] if summary else []
        rid = str(uuid.uuid4())

        def _run():
            for key in keys:
                ok, msg = ai_providers.validate_key(provider, key)
                settings_mod.record_key_test(provider, key, ok, msg)
                emit("key_validated", {"request_id": rid, "ok": ok, "message": msg, "provider": provider, "key": key})
            emit("test_all_completed", {"request_id": rid, "provider": provider, "total": len(keys)})

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "request_id": rid, "total": len(keys)}

    # ---- Embed button on Meta Generator, real small popup (v0.8.3) ----
    # Reappeared: this was present pre-JS-migration ("Embed button, to
    # the left of Clear All, shown only after a full natural generation
    # completion") but got dropped while porting the UI layer over.
    # Restored using the same real popup-window mechanism as the API
    # Manager button above -- shares this same Api instance/session, so
    # it always reflects the exact batch that just finished.
    def get_embed_readiness(self):
        s = self.session
        return {"ok": True, "ready": bool(s.batch_complete and s.last_csv_path)}

    def auto_load_embed(self):
        """Called by the Embed popup right after it opens: loads the
        just-written working CSV and the batch's common folder straight
        in, no manual 'Load CSV…' / 'Select folder…' click needed."""
        s = self.session
        if not s.last_csv_path or not os.path.exists(s.last_csv_path):
            return {"ok": False, "error": "No completed batch to embed yet."}
        res = self.embed_session.load_csv(s.last_csv_path)
        folder = s.last_common_folder or res.get("guessed_folder")
        if folder:
            self.embed_session.set_folder(folder)
        res["folder"] = folder
        return res

    def open_embed_popup(self):
        if frontend_dir is None:
            return {"ok": False, "error": "not ready"}
        popup = webview.create_window(
            "Meta Embedder",
            url=os.path.join(frontend_dir, "index.html") + "?popup=embed&auto=1",
            js_api=self,
            width=560,
            height=700,
            min_size=(480, 560),
        )
        # Same shared-Api-instance pattern as open_api_manager_popup --
        # a smaller *view* of the same app state, not a second app.
        threading.Thread(target=start_event_drain, args=(popup,), daemon=True).start()
        return {"ok": True}

    # ---- Dashboard (Stage 5 batch 2: real dashboard.py aggregation) ----
    def get_dashboard_data(self):
        return {"ok": True, **dashboard_mod.get_dashboard_data(self.session)}

    def set_daily_limit(self, value):
        return dashboard_mod.set_daily_limit(value)

    # ---- Global status bar (bottom of every page, matches original) ----
    def get_status_bar(self):
        exif_ok = False
        try:
            from core.utils import find_exiftool
            exif_ok = bool(find_exiftool())
        except Exception:
            pass
        return {
            "ok": True,
            "exiftool_ready": exif_ok,
            "meta_running": self.session.running,
        }

    # ---- Prompt-to-Prompt (v0.8.7: both From Text and From Image modes
    # wired to the real engine; use_image selects which) ----
    def start_prompt_to_prompt(self, original_prompt, count, creativity, style,
                                target_words=None, concurrency=3, use_image=False):
        return self.p2p_session.start(original_prompt, count, creativity, style,
                                       target_words, concurrency, source_image=use_image)

    def pause_prompt_to_prompt(self):
        return self.p2p_session.pause()

    def stop_prompt_to_prompt(self):
        return self.p2p_session.stop()

    # ---- P2P "From Image" reference slots (up to 15) ----
    def browse_p2p_images(self):
        if self._window is None:
            return {"ok": False, "error": "window not ready"}
        remaining = 15 - len(self.p2p_session.images.paths)
        if remaining <= 0:
            return {"ok": True, "accepted": [], "rejected": [], "paths": list(self.p2p_session.images.paths)}
        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=(
                "Image files (*.jpg;*.jpeg;*.png;*.webp;*.gif;*.tiff;*.tif)",
                "All files (*.*)",
            ),
        )
        if not paths:
            return {"ok": True, "accepted": [], "rejected": [], "paths": list(self.p2p_session.images.paths)}
        result = self.p2p_session.images.add_paths(list(paths))
        return {"ok": True, **result}

    def get_p2p_image_thumb(self, path):
        return {"ok": True, "thumb": self.p2p_session.images.thumb_cache.get(path)}

    def get_p2p_images(self):
        return {"ok": True, "paths": list(self.p2p_session.images.paths)}

    def remove_p2p_image(self, path):
        return {"ok": True, "paths": self.p2p_session.images.remove(path)}

    def clear_p2p_images(self):
        self.p2p_session.images.clear()
        return {"ok": True}

    def add_p2p_images_dropped(self, paths):
        """Real-path drag-and-drop onto the 15-slot image grid, called
        from app.py's element-scoped drop handler."""
        result = self.p2p_session.images.add_paths(list(paths))
        return {"ok": True, **result}

    # ---- Demo of a long-running task using the REAL queue seam ----
    def start_demo_batch(self, n=8):
        """Not fake progress math -- reuses ui_event_queue exactly as
        a real generation batch will in Stage 4/5, so the JS progress
        bar code being written now doesn't need to change later."""
        def _work():
            for i in range(n):
                time.sleep(0.12)
                emit("task_progress", {"done": i + 1, "total": n})
            emit("task_completed", {"total": n})

        threading.Thread(target=_work, daemon=True).start()
        return {"ok": True, "started": True}

    # ---- Batch (formerly "Automation Queue -- BETA") ----
    # Now a normal in-window nav page (id "automation" internally,
    # labeled "Batch" in the sidebar) instead of a separate popup
    # window -- no open_automation_window/webview.create_window call
    # here anymore. Real filesystem drag-and-drop for #automationDropzone
    # is bound on the main window itself, in app.py's
    # _bind_real_drag_drop, the same way every other in-window dropzone
    # (Embed's CSV/folder drops, P2P's image grid) already works.
    def automation_get_state(self):
        return {"ok": True, **self.automation_queue.to_dict(),
                "controller": self.automation_controller.status_dict()}

    def automation_start_queue(self):
        return self.automation_controller.start()

    def automation_pause_queue(self):
        return self.automation_controller.pause()

    def automation_stop_queue(self):
        return self.automation_controller.stop()

    def automation_get_presets(self):
        return self.automation_queue.get_presets()

    def automation_browse_folder(self):
        """'+ Add Folder' button -- native picker, same dialog mechanism
        as browse_embed_folder above."""
        if self._window is None:
            return {"ok": False, "error": "window not ready"}
        paths = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not paths:
            return {"ok": False, "cancelled": True}
        return self.automation_queue.add_folder(paths[0])

    def automation_add_folder(self, path, preset_name="Custom"):
        return self.automation_queue.add_folder(path, preset_name)

    def automation_add_folders_dropped(self, paths):
        """Accepts real folder paths from the Batch page's drop zone
        (see app.py's _bind_real_drag_drop). Mirrors
        set_embed_folder_dropped's tolerance: if the OS handed over a
        file path instead of a folder, falls back to that file's
        parent folder rather than rejecting the drop outright."""
        folders = []
        for p in paths:
            if os.path.isdir(p):
                folders.append(p)
            elif os.path.isfile(p):
                folders.append(os.path.dirname(p))
        # de-dupe while preserving drop order
        seen = set()
        ordered = [f for f in folders if not (f in seen or seen.add(f))]
        return self.automation_queue.add_folders(ordered)

    def automation_remove_folder(self, job_id):
        return self.automation_queue.remove_folder(job_id)

    def automation_duplicate_folder(self, job_id):
        return self.automation_queue.duplicate_folder(job_id)

    def automation_reorder(self, ordered_ids):
        return self.automation_queue.reorder(ordered_ids)

    def automation_move(self, job_id, direction):
        return self.automation_queue.move(job_id, direction)

    def automation_update_job_options(self, job_id, options):
        return self.automation_queue.update_job_options(job_id, options)

    def automation_apply_preset(self, job_id, preset_name):
        return self.automation_queue.apply_preset(job_id, preset_name)

    def automation_save_custom_preset(self, name, options):
        return self.automation_queue.save_custom_preset(name, options)

    def automation_apply_universal_preset(self, options):
        """Universal Preset: apply one set of settings, defined once,
        to every folder currently in the Batch queue -- individual
        per-folder customization (automation_update_job_options /
        automation_apply_preset above) still works afterward on top of
        it."""
        return self.automation_queue.apply_universal_preset(options)

    def automation_check_resumable(self):
        data = self.automation_queue.find_resumable()
        return {"ok": True, "resumable": data is not None, "data": data}

    def automation_load_resumable(self):
        data = self.automation_queue.find_resumable()
        if not data:
            return {"ok": False, "error": "Nothing to resume."}
        return {"ok": True, **self.automation_queue.load_from_state(data)}

    def automation_discard_resumable(self):
        return self.automation_queue.discard_saved_state()

    def automation_dry_run(self):
        return self.automation_queue.dry_run()

    def automation_clear_all(self):
        """Batch page's Clear All (distinct from Meta Generator's own
        clear_batch/#btnClear above -- this only ever touches the
        Batch queue). Resets every queued folder, their per-folder
        job/progress state, the persisted resume snapshot, and the
        controller's progress/report state in one action, instead of
        the frontend looping automation_remove_folder per folder.
        Never touches the user's actual files/folders on disk -- only
        MetaZone's own queue/cache state.

        Blocked while the queue is actively running, same precaution
        as remove_folder refusing to drop a folder mid-processing:
        the frontend should Stop the queue first."""
        if self.automation_controller.running:
            return {"ok": False, "error": "Can't clear while the queue is running. Stop it first."}
        self.automation_controller.reset()
        return self.automation_queue.clear_all()
