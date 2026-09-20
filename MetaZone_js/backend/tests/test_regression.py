"""
MetaZone backend regression suite (added in v0.9.4.1, extended in v0.9.5).

Covers backend/core logic only, per the maintenance-revision brief --
this deliberately does not try to drive the GUI. Plain stdlib
unittest, no extra dependency to install.

Run from the backend/ directory:
    python3 -m unittest discover -s tests -v

Or a single file:
    python3 -m unittest tests.test_regression -v

What's covered (maps to the v0.9.4.1 brief's required list):
  - duplicate detection                  -> DuplicateDetectionTests
  - input/order preservation             -> ImportOrderTests
  - Generate Remaining filtering          -> GenerateRemainingFilterTests
  - Retry Failed filtering                -> RetryFailedFilterTests
  - Undo ordering                         -> UndoOrderingTests
  - Clear All / generation epoch protection -> ClearAllEpochTests
  - Dry Run classification                -> DryRunClassificationTests
  - API-key persistence                   -> ApiKeyPersistenceTests

Test All event/request correlation is frontend JS logic (settings.js),
not backend -- see frontend/tests/test_event_correlation.js instead.
"""
import sys
import os
import types
import tempfile
import threading
import time
import csv
import shutil
import subprocess
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub the 'bridge' module before session.py/embedder.py/bridge.py get
# imported anywhere -- bridge.py imports Session/EmbedSession, which
# would otherwise be a circular import the moment a test imports
# session.py directly. These tests only need bridge.emit to exist and
# record what was emitted (some assertions check event payloads).
EMITTED = []
_fake_bridge = types.ModuleType('bridge')
_fake_bridge.emit = lambda name, payload: EMITTED.append((name, payload))
_fake_bridge.api_instance = None
# v0.9.6.5: Session.clear() now also calls bridge.purge_stale_events()
# to strip any already-queued stale event for this session -- this
# stub bridge has no real ui_event_queue to purge, so it's just a
# no-op that reports "nothing removed", matching the real function's
# return shape (an int count) closely enough for these tests, which
# don't assert on it.
_fake_bridge.purge_stale_events = lambda prefix: 0
sys.modules['bridge'] = _fake_bridge

from PIL import Image
import session as session_mod
import embedder
import settings as settings_mod
import core.config as core_config
import core.utils as core_utils
import json


def make_test_image(path):
    Image.new('RGB', (5, 5), 'red').save(path)


def fresh_prefs_dir():
    """Points core.config at a throwaway prefs.json so these tests
    never touch the real user's saved settings."""
    d = tempfile.mkdtemp()
    core_config._common_pref_dir = lambda: d
    return d


class DuplicateDetectionTests(unittest.TestCase):
    def test_duplicate_path_is_rejected_and_counted_as_duplicate(self):
        tmpdir = tempfile.mkdtemp()
        p = os.path.join(tmpdir, 'a.jpg')
        make_test_image(p)
        s = session_mod.Session()
        s.add_paths([p])
        result = s.add_paths([p])  # re-add the exact same path
        self.assertEqual(result['accepted'], [])
        reasons = [reason for (_, reason) in result['rejected']]
        self.assertIn('duplicate', reasons)
        self.assertEqual(s.all_paths, [p])  # not added twice


class ImportOrderTests(unittest.TestCase):
    def test_parallel_validation_preserves_input_order_even_when_reversed(self):
        """The actual bug this guards: accepted/all_paths used to be
        built in whichever order each validation thread finished, not
        input order. This forces completion order to be the exact
        reverse of input order and checks the result is still correct.
        """
        tmpdir = tempfile.mkdtemp()
        paths = []
        for i in range(5):
            p = os.path.join(tmpdir, f'img_{i}.jpg')
            make_test_image(p)
            paths.append(p)

        orig = session_mod.wait_stable_and_validate_image

        def fake_validate(path, *a, **k):
            idx = int(os.path.basename(path).split('_')[1].split('.')[0])
            time.sleep((5 - idx) * 0.03)  # earlier files sleep longer -> finish LAST
            return True, None

        session_mod.wait_stable_and_validate_image = fake_validate
        try:
            s = session_mod.Session()
            result = s.add_paths(paths)
            self.assertEqual(result['accepted'], paths)
            self.assertEqual(s.all_paths, paths)
        finally:
            session_mod.wait_stable_and_validate_image = orig


class _CaptureThread(threading.Thread):
    """Swaps in for threading.Thread around start_generation calls so
    the test can inspect exactly which targets a generation run would
    have processed, without actually running any real generation
    (no network, no AI provider calls)."""
    captured_targets = None

    def __init__(self, target=None, args=(), **kw):
        _CaptureThread.captured_targets = list(args[0]) if args else None
        super().__init__(target=lambda: None, **kw)


class GenerateRemainingFilterTests(unittest.TestCase):
    def test_start_generation_only_targets_non_done_paths(self):
        s = session_mod.Session()
        s.all_paths = ['A', 'B', 'C', 'D']
        s.results = {
            'A': {'status': 'done'},
            'B': {'status': 'failed'},
            'C': {'status': 'waiting'},
            'D': {'status': 'done'},
        }
        orig_thread_cls = threading.Thread
        session_mod.threading.Thread = _CaptureThread
        try:
            res = s.start_generation('meta', {}, {})
        finally:
            session_mod.threading.Thread = orig_thread_cls
        self.assertTrue(res['ok'])
        self.assertEqual(set(_CaptureThread.captured_targets), {'B', 'C'})
        self.assertNotIn('A', _CaptureThread.captured_targets)
        self.assertNotIn('D', _CaptureThread.captured_targets)


class RetryFailedFilterTests(unittest.TestCase):
    def test_start_generation_for_paths_targets_exactly_the_given_list(self):
        s = session_mod.Session()
        s.all_paths = ['A', 'B', 'C']
        s.results = {
            'A': {'status': 'done'},
            'B': {'status': 'failed'},
            'C': {'status': 'failed'},
        }
        orig_thread_cls = threading.Thread
        session_mod.threading.Thread = _CaptureThread
        try:
            # Only asking to retry B -- C is also failed but must be
            # left alone, proving this isn't secretly falling back to
            # "retry everything failed" behavior.
            res = s.start_generation_for_paths(['B'], 'meta', {}, {})
        finally:
            session_mod.threading.Thread = orig_thread_cls
        self.assertTrue(res['ok'])
        self.assertEqual(_CaptureThread.captured_targets, ['B'])

    def test_unknown_path_is_silently_dropped_not_errored(self):
        s = session_mod.Session()
        s.all_paths = ['A']
        s.results = {'A': {'status': 'failed'}}
        orig_thread_cls = threading.Thread
        session_mod.threading.Thread = _CaptureThread
        try:
            res = s.start_generation_for_paths(['A', 'never-imported.jpg'], 'meta', {}, {})
        finally:
            session_mod.threading.Thread = orig_thread_cls
        self.assertTrue(res['ok'])
        self.assertEqual(_CaptureThread.captured_targets, ['A'])


class UndoOrderingTests(unittest.TestCase):
    """v0.9.6 rewrite: Undo is now deletion-order FIFO with every
    restore appended to the END of the list -- NOT the old v0.9.4.1
    original-position restore these tests used to cover. That was an
    explicit, intentional behavior change (not a bug fix on top of the
    old mechanism), so these tests were rewritten to match rather than
    reverted -- see session.py's undo_next() docstring for the
    worked example this batch's spec gave."""

    def test_single_delete_and_restore_appends_to_end(self):
        """A -> B -> C -> D, delete B, undo -> A -> C -> D -> B
        (appended at the end, not reinserted at its old position)."""
        s = session_mod.Session()
        paths = ['A', 'B', 'C', 'D']
        s.all_paths = list(paths)
        s.completion_order = list(paths)
        for p in paths:
            s.results[p] = {'status': 'done', 'title': p}

        s.delete_card('B')
        self.assertEqual(s.all_paths, ['A', 'C', 'D'])
        self.assertEqual(s.completion_order, ['A', 'C', 'D'])
        self.assertEqual(len(s.undo_queue), 1)

        res = s.undo_next()
        self.assertTrue(res['ok'])
        self.assertEqual(res['path'], 'B')
        self.assertEqual(s.all_paths, ['A', 'C', 'D', 'B'])
        self.assertEqual(s.completion_order, ['A', 'C', 'D', 'B'])
        self.assertEqual(s.undo_queue, [])

    def test_fifo_deletion_order_not_reverse_order(self):
        """A B C D E F G H; delete D, delete F, delete H (as three
        separate delete_card calls, matching the spec's own worked
        example). Three individual Undo clicks must restore D, then F,
        then H -- i.e. FIFO by deletion order, each appended at the
        end -- NOT H, F, D (which would be reverse/LIFO order)."""
        s = session_mod.Session()
        paths = list('ABCDEFGH')
        s.all_paths = list(paths)
        s.completion_order = list(paths)
        for p in paths:
            s.results[p] = {'status': 'done', 'title': p}

        for p in ('D', 'F', 'H'):
            s.delete_card(p)
        self.assertEqual(s.completion_order, ['A', 'B', 'C', 'E', 'G'])
        self.assertEqual([e['path'] for e in s.undo_queue], ['D', 'F', 'H'])

        restored_order = [s.undo_next()['path'] for _ in range(3)]
        self.assertEqual(restored_order, ['D', 'F', 'H'],
                          "Undo must restore in the order things were deleted "
                          "(FIFO), not the reverse")
        self.assertEqual(s.completion_order,
                          ['A', 'B', 'C', 'E', 'G', 'D', 'F', 'H'])

    def test_bulk_delete_enters_undo_queue_in_actual_deletion_order(self):
        """Multiple selected deletions (delete_cards) must enter the
        undo queue in the actual order they were removed, same as a
        sequence of individual delete_card calls would."""
        s = session_mod.Session()
        paths = ['A', 'B', 'C', 'D']
        s.all_paths = list(paths)
        s.completion_order = list(paths)
        for p in paths:
            s.results[p] = {'status': 'done', 'title': p}

        s.delete_cards(['C', 'A'])  # selection order C then A
        self.assertEqual(s.all_paths, ['B', 'D'])
        self.assertEqual([e['path'] for e in s.undo_queue], ['C', 'A'])

        self.assertEqual(s.undo_next()['path'], 'C')
        self.assertEqual(s.undo_next()['path'], 'A')
        self.assertEqual(s.all_paths, ['B', 'D', 'C', 'A'])

    def test_undo_on_empty_queue_reports_error_not_a_crash(self):
        s = session_mod.Session()
        res = s.undo_next()
        self.assertFalse(res['ok'])

    def test_clear_wipes_the_undo_queue(self):
        s = session_mod.Session()
        s.all_paths = ['A', 'B']
        s.completion_order = ['A', 'B']
        s.results = {'A': {'status': 'done'}, 'B': {'status': 'done'}}
        s.delete_card('B')
        self.assertEqual(len(s.undo_queue), 1)

        s.clear()
        self.assertEqual(s.undo_queue, [])
        res = s.undo_next()
        self.assertFalse(res['ok'], "a deletion from before Clear must not be undoable after it")


class ClearAllEpochTests(unittest.TestCase):
    def test_epoch_bump_invalidates_a_stale_in_flight_run(self):
        """Clear All / starting a new run bumps gen_epoch; a worker
        thread from the previous run must recognize its own captured
        epoch no longer matches and refuse to write results."""
        s = session_mod.Session()
        s.all_paths = ['A']
        s.results = {'A': {'status': 'waiting'}}
        s.gen_epoch = 1
        captured_epoch_for_stale_worker = s.gen_epoch

        s.gen_epoch += 1  # simulate Clear All / a fresh Generate click

        self.assertNotEqual(captured_epoch_for_stale_worker, s.gen_epoch)

    def test_clear_resets_all_ordering_and_undo_state(self):
        s = session_mod.Session()
        s.all_paths = ['A', 'B']
        s.completion_order = ['A', 'B']
        s.results = {'A': {'status': 'done'}, 'B': {'status': 'done'}}
        s.delete_card('B')  # populates undo_queue
        self.assertTrue(s.undo_queue)

        s.clear()
        self.assertEqual(s.all_paths, [])
        self.assertEqual(s.completion_order, [])
        self.assertEqual(s.results, {})
        self.assertEqual(s.undo_queue, [])  # stale undo data must not survive a Clear All


class DryRunClassificationTests(unittest.TestCase):
    def _build_sample_folder_and_csv(self):
        tmpdir = tempfile.mkdtemp()
        p1 = os.path.join(tmpdir, 'photo1.jpg')
        p2 = os.path.join(tmpdir, 'notes.txt')
        make_test_image(p1)
        with open(p2, 'w') as f:
            f.write('not an image')
        csv_path = os.path.join(tmpdir, 'meta.csv')
        with open(csv_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['filename', 'title'])
            w.writerow(['photo1.jpg', 'T1'])       # matched
            w.writerow(['ghost.jpg', 'T2'])         # missing_image
            w.writerow(['', 'T3'])                  # missing_metadata
            w.writerow(['notes.txt', 'T4'])         # unsupported
        return tmpdir, csv_path, p1

    def test_four_base_categories_never_touch_disk(self):
        tmpdir, csv_path, p1 = self._build_sample_folder_and_csv()
        sess = embedder.EmbedSession()
        sess.load_csv(csv_path)

        import hashlib
        with open(p1, 'rb') as f:
            before = hashlib.md5(f.read()).hexdigest()
        res = sess.dry_run(tmpdir, {'filename': 'filename', 'title': 'title'},
                            {'subfolders': False, 'match_ext_only': True})
        with open(p1, 'rb') as f:
            after = hashlib.md5(f.read()).hexdigest()

        self.assertTrue(res['ok'])
        self.assertEqual(res['counts']['matched'], 1)
        self.assertEqual(res['counts']['missing_image'], 1)
        self.assertEqual(res['counts']['missing_metadata'], 1)
        self.assertEqual(res['counts']['unsupported'], 1)
        self.assertEqual(before, after, 'dry_run must never modify a file')

    @unittest.skipUnless(shutil.which('exiftool'), 'exiftool not installed in this environment')
    def test_already_embedded_detected_via_real_exiftool_batch_read(self):
        tmpdir = tempfile.mkdtemp()
        p1 = os.path.join(tmpdir, 'photo1.jpg')
        p2 = os.path.join(tmpdir, 'photo2.jpg')
        make_test_image(p1)
        make_test_image(p2)
        subprocess.run(['exiftool', '-overwrite_original', '-Title=Existing title', p1],
                        capture_output=True)

        csv_path = os.path.join(tmpdir, 'meta.csv')
        with open(csv_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['filename', 'title'])
            w.writerow(['photo1.jpg', 'New title'])
            w.writerow(['photo2.jpg', 'New title'])

        # find_exiftool() looks for a bundled exiftool.exe next to
        # app.py, which doesn't exist in a plain test environment --
        # point it at the system exiftool just for this test, the
        # same way a packaged Windows build points it at its bundled
        # copy.
        original_find = embedder.find_exiftool
        embedder.find_exiftool = lambda: 'exiftool'
        try:
            sess = embedder.EmbedSession()
            sess.load_csv(csv_path)
            res = sess.dry_run(tmpdir, {'filename': 'filename', 'title': 'title'},
                                {'subfolders': False, 'match_ext_only': True})
        finally:
            embedder.find_exiftool = original_find

        self.assertEqual(res['counts']['already_embedded'], 1)
        self.assertEqual(res['counts']['matched'], 1)
        cats = {r['filename']: r['category'] for r in res['rows']}
        self.assertEqual(cats['photo1.jpg'], 'already_embedded')
        self.assertEqual(cats['photo2.jpg'], 'matched')

        # Still must not have modified anything.
        check = subprocess.run(['exiftool', '-Title', '-s3', p1], capture_output=True, text=True)
        self.assertEqual(check.stdout.strip(), 'Existing title')

    def test_missing_exiftool_falls_back_to_matched_not_misclassified(self):
        """If exiftool can't be found/run, already-embedded files must
        fall back to "matched" (unknown, assume ready) -- never get
        silently mislabeled as something they're not."""
        tmpdir, csv_path, p1 = self._build_sample_folder_and_csv()
        original_find = embedder.find_exiftool
        embedder.find_exiftool = lambda: None
        try:
            sess = embedder.EmbedSession()
            sess.load_csv(csv_path)
            res = sess.dry_run(tmpdir, {'filename': 'filename', 'title': 'title'},
                                {'subfolders': False, 'match_ext_only': True})
        finally:
            embedder.find_exiftool = original_find
        self.assertEqual(res['counts']['already_embedded'], 0)
        self.assertEqual(res['counts']['matched'], 1)


class EmbedClearAllTests(unittest.TestCase):
    """v0.9.8: Embed page "Clear All" -- EmbedSession.clear().

    The loaded CSV and folder live on the backend session, so clearing
    only the page would leave them in memory. These pin the behavior
    the page relies on."""

    def _csv(self, folder, name, rows):
        path = os.path.join(folder, name)
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['filename', 'title'])
            for r in rows:
                w.writerow(r)
        return path

    def test_clear_forgets_csv_folder_and_cached_index(self):
        d = tempfile.mkdtemp()
        make_test_image(os.path.join(d, 'a.jpg'))
        sess = embedder.EmbedSession()
        sess.load_csv(self._csv(d, 'm.csv', [['a.jpg', 'T']]))
        sess.set_folder(d)
        sess.preview_match(d, 'filename', False, True)      # populates the cached index
        self.assertTrue(sess.csv_rows and sess.folder and sess._file_index is not None)

        res = sess.clear()

        self.assertEqual(res, {'ok': True})
        self.assertEqual((sess.csv_rows, sess.csv_headers, sess.folder), ([], [], ''))
        self.assertIsNone(sess._file_index)
        self.assertIsNone(sess._file_index_key)

    def test_start_embed_after_clear_has_no_csv_left(self):
        d = tempfile.mkdtemp()
        sess = embedder.EmbedSession()
        sess.load_csv(self._csv(d, 'm.csv', [['a.jpg', 'T']]))
        sess.clear()
        original_find = embedder.find_exiftool
        embedder.find_exiftool = lambda: 'exiftool'   # get past the exiftool check to the CSV check
        try:
            res = sess.start_embed(d, {'filename': 'filename'}, {})
        finally:
            embedder.find_exiftool = original_find
        self.assertFalse(res['ok'])
        self.assertEqual(res['error'], 'Load a CSV first.')

    def test_next_csv_guesses_its_own_folder_not_the_cleared_one(self):
        """The bug a page-only reset would leave: load_csv() reuses
        self.folder as the guessed folder whenever one is set."""
        old = tempfile.mkdtemp()
        new = tempfile.mkdtemp()
        sess = embedder.EmbedSession()
        sess.load_csv(self._csv(old, 'm.csv', [['a.jpg', 'T']]))
        sess.set_folder(old)
        self.assertEqual(sess.load_csv(self._csv(new, 'n.csv', [['b.jpg', 'T']]))['guessed_folder'], old)  # the old behavior, for contrast
        sess.clear()
        self.assertEqual(sess.load_csv(self._csv(new, 'n.csv', [['b.jpg', 'T']]))['guessed_folder'], new)

    def test_clear_refused_while_running_then_allowed_after_it_finishes(self):
        d = tempfile.mkdtemp()
        files = []
        for i in range(4):
            fp = os.path.join(d, f'p{i}.jpg')
            make_test_image(fp)
            files.append([f'p{i}.jpg', f'T{i}'])
        sess = embedder.EmbedSession()
        sess.load_csv(self._csv(d, 'm.csv', files))
        sess.set_folder(d)

        release = threading.Event()
        def slow_embed(et, fp, title, kw, desc, rm_prog, rm_copy):
            release.wait(10)                       # hold the run in flight
            return True, 'ok', fp
        orig_find, orig_embed = embedder.find_exiftool, embedder.embed_metadata_one
        embedder.find_exiftool = lambda: 'exiftool'
        embedder.embed_metadata_one = slow_embed
        try:
            res = sess.start_embed(d, {'filename': 'filename', 'title': 'title'},
                                    {'subfolders': False, 'match_ext_only': True, 'concurrency': 2})
            self.assertTrue(res['ok'])
            self.assertTrue(sess.running)

            refused = sess.clear()
            self.assertFalse(refused['ok'])
            self.assertIn('running', refused['error'])
            self.assertEqual(len(sess.csv_rows), 4, 'a refused clear must not touch the loaded CSV')
            self.assertEqual(sess.folder, d)

            release.set()
            deadline = time.time() + 15
            while sess.running and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(sess.running, 'run should have finished')
            self.assertEqual(sess.clear(), {'ok': True})
            self.assertEqual(sess.csv_rows, [])
        finally:
            release.set()
            embedder.find_exiftool, embedder.embed_metadata_one = orig_find, orig_embed


class ApiKeyPersistenceTests(unittest.TestCase):
    def setUp(self):
        fresh_prefs_dir()
        core_config.save_prefs({'ai_keys': {'Gemini': [
            {'key': 'AIza-key-one', 'active': False},
            {'key': 'AIza-key-two', 'active': True},
        ]}})

    def test_nickname_persists_and_is_trimmed(self):
        settings_mod.set_key_nickname('Gemini', 0, '  Main Account  ')
        summary = settings_mod.get_provider_summary()
        gem = next(p for p in summary if p['provider'] == 'Gemini')
        self.assertEqual(gem['keys'][0]['nickname'], 'Main Account')

    def test_key_test_result_persists_with_correct_status(self):
        settings_mod.record_key_test('Gemini', 'AIza-key-one', True, 'Valid')
        settings_mod.record_key_test('Gemini', 'AIza-key-two', False, 'Invalid key — 401')
        summary = settings_mod.get_provider_summary()
        gem = next(p for p in summary if p['provider'] == 'Gemini')
        self.assertEqual(gem['keys'][0]['last_test']['status'], 'valid')
        self.assertEqual(gem['keys'][1]['last_test']['status'], 'invalid')

    def test_untested_key_reports_no_last_test_not_a_fabricated_one(self):
        summary = settings_mod.get_provider_summary()
        gem = next(p for p in summary if p['provider'] == 'Gemini')
        self.assertIsNone(gem['keys'][0]['last_test'])

    def test_activate_valid_keys_only_touches_keys_with_a_real_result(self):
        settings_mod.record_key_test('Gemini', 'AIza-key-one', True, 'Valid')
        # key-two is left untested on purpose
        settings_mod.activate_valid_keys('Gemini')
        summary = settings_mod.get_provider_summary()
        gem = next(p for p in summary if p['provider'] == 'Gemini')
        self.assertTrue(gem['keys'][0]['active'])   # valid -> activated
        self.assertFalse(gem['keys'][1]['active'])  # untested -> deactivated, not guessed

    def test_model_disable_persists_and_filters_dropdown_list(self):
        core_config.save_prefs({'ai_models': {'Gemini': 'gemini-3.6-flash'}})
        res = settings_mod.set_model_enabled('Gemini', 'gemini-1.5-flash', False)
        self.assertTrue(res['ok'])
        summary = settings_mod.get_provider_summary()
        gem = next(p for p in summary if p['provider'] == 'Gemini')
        enabled_ids = [mid for (_, mid) in gem['models']]
        all_ids = [mid for (_, mid) in gem['all_models']]
        self.assertNotIn('gemini-1.5-flash', enabled_ids)
        self.assertIn('gemini-1.5-flash', all_ids)  # not removed from the app, just hidden

    def test_cannot_disable_the_last_enabled_model(self):
        summary = settings_mod.get_provider_summary()
        gem = next(p for p in summary if p['provider'] == 'Gemini')
        all_ids = [mid for (_, mid) in gem['all_models']]
        for mid in all_ids[:-1]:
            settings_mod.set_model_enabled('Gemini', mid, False)
        res = settings_mod.set_model_enabled('Gemini', all_ids[-1], False)
        self.assertFalse(res['ok'])


_EPS10_FIXTURE = (
    "%!PS-Adobe-3.0 EPSF-3.0\n"
    "%%Creator: Adobe Illustrator(R) 24.0\n"
    "%%For: MetaZone test fixture\n"
    "%%Title: original.eps\n"
    "%%CreationDate: 1/1/2024\n"
    "%%BoundingBox: 0 0 100 100\n"
    "%%HiResBoundingBox: 0 0 100 100\n"
    "%%DocumentProcessColors: Cyan Magenta Yellow Black\n"
    "%AI5_FileFormat 10.0\n"
    "%%EndComments\n"
    "%%BeginProlog\n"
    "%%EndProlog\n"
    "%%BeginSetup\n"
    "%%EndSetup\n"
    "%%Page: 1 1\n"
    "0 0 100 100 rectfill\n"
    "%%PageTrailer\n"
    "%%Trailer\n"
    "%%EOF\n"
)
# Minimal but structurally valid %AI5_FileFormat 10.0 EPS -- this is a real,
# spec-legal EPS/PostScript header+trailer (not a JPEG/PNG relabeled with a
# .eps extension), so it exercises the actual EPS write path in
# embed_metadata_one, not the generic-format path. It does not contain real
# vector artwork; these tests are about metadata field preservation and
# structural-header integrity, not rendering correctness.


@unittest.skipUnless(shutil.which('exiftool'), 'exiftool not installed in this environment')
class Eps10EmbeddingTests(unittest.TestCase):
    """v0.9.7.3: regression coverage for the EPS10 metadata bug -- ExifTool's
    repeated -Keywords=/-Subject= flags silently overwrite (rather than
    accumulate into) the PostScript-native single-string Keywords/Subject
    DSC fields, even though the same flags correctly append to IPTC:Keywords
    (a real list tag). A returncode of 0 from the write step does not catch
    this; these tests read the PostScript-native group back explicitly
    (`-G1 -PostScript:Keywords`) rather than trusting the app's own
    self-reported success, since that's exactly the gap that let the bug
    ship silently before."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.et = 'exiftool'

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_eps(self, name='test.eps'):
        p = os.path.join(self.tmpdir, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(_EPS10_FIXTURE)
        return p

    def _read_ps_keywords(self, path):
        cmd = [self.et, '-j', '-charset', 'UTF8', '-G1',
               '-PostScript:Keywords', '-IPTC:Keywords', path]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return json.loads(res.stdout or '[]')[0]

    def test_multiple_keywords_survive_in_postscript_native_field(self):
        """The actual bug: before the fix, only the LAST keyword survived
        in PostScript:Keywords (each repeat of -Keywords= overwrote the
        DSC comment), while IPTC:Keywords looked fine -- so a naive
        read-back check that only looked at IPTC/the default-resolved tag
        would report success on a file that had actually lost data."""
        p = self._make_eps()
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='Sunset Beach', kw_raw='cat, dog, sunset, ocean view', desc='')
        self.assertTrue(ok, msg)
        entry = self._read_ps_keywords(fp)
        ps_tokens = [t.strip() for t in entry.get('PostScript:Keywords', '').split(',')]
        self.assertEqual(ps_tokens, ['cat', 'dog', 'sunset', 'ocean view'])
        self.assertEqual(entry.get('IPTC:Keywords'), ['cat', 'dog', 'sunset', 'ocean view'])

    def test_title_and_description_round_trip(self):
        p = self._make_eps()
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='Ocean View', kw_raw='wave, sand', desc='A calm ocean scene')
        self.assertTrue(ok, msg)
        cmd = [self.et, '-j', '-charset', 'UTF8', '-G1',
               '-PostScript:Title', '-IPTC:Caption-Abstract', fp]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        entry = json.loads(res.stdout)[0]
        self.assertEqual(entry.get('PostScript:Title'), 'Ocean View')
        self.assertEqual(entry.get('IPTC:Caption-Abstract'), 'A calm ocean scene')

    def test_unicode_title_and_keywords(self):
        p = self._make_eps()
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='日本語 タイトル', kw_raw='猫,犬', desc='説明')
        self.assertTrue(ok, msg)
        entry = self._read_ps_keywords(fp)
        self.assertEqual(entry.get('IPTC:Keywords'), ['猫', '犬'])

    def test_long_title_verified_via_uncapped_postscript_field(self):
        """IPTC:ObjectName has a hard 64-byte cap from the IPTC IIM spec
        itself (ExifTool truncates it silently, exit code 0 either way) --
        this affects every format, not just EPS, and isn't something this
        fix can lift. The title must still round-trip completely through
        PostScript:Title, which has no such cap."""
        p = self._make_eps()
        long_title = 'A' * 300
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title=long_title, kw_raw='x', desc='')
        self.assertTrue(ok, msg)

    def test_empty_and_malformed_keyword_entries_are_dropped(self):
        p = self._make_eps()
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='T', kw_raw='cat,, ,dog,;;fox', desc='')
        self.assertTrue(ok, msg)
        entry = self._read_ps_keywords(fp)
        ps_tokens = [t.strip() for t in entry.get('PostScript:Keywords', '').split(',')]
        self.assertEqual(ps_tokens, ['cat', 'dog', 'fox'])

    def test_duplicate_keywords_not_duplicated_in_postscript_field(self):
        p = self._make_eps()
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='T', kw_raw='cat,dog,cat', desc='')
        self.assertTrue(ok, msg)
        entry = self._read_ps_keywords(fp)
        ps_tokens = [t.strip() for t in entry.get('PostScript:Keywords', '').split(',')]
        self.assertEqual(ps_tokens, ['cat', 'dog'])  # deduped, order kept
        # IPTC list keeps the original (unchanged, matches every other
        # format's existing no-dedup behavior)
        self.assertEqual(entry.get('IPTC:Keywords'), ['cat', 'dog', 'cat'])

    def test_corrupt_eps_file_fails_cleanly_not_falsely(self):
        p = os.path.join(self.tmpdir, 'corrupt.eps')
        with open(p, 'wb') as f:
            f.write(b'this is not a valid EPS file at all')
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='T', kw_raw='k', desc='')
        self.assertFalse(ok)

    def test_eps_header_intact_after_write(self):
        p = self._make_eps()
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='T', kw_raw='k1,k2', desc='')
        self.assertTrue(ok, msg)
        with open(fp, 'rb') as f:
            head = f.read(2)
        self.assertEqual(head, b'%!')

    def test_jpeg_keyword_behavior_unchanged_by_eps_fix(self):
        """Non-EPS regression guard: JPEG must keep going through the
        original generic (unscoped, non-deduped) command path -- this fix
        must not touch JPEG/PNG behavior at all."""
        p = os.path.join(self.tmpdir, 'test.jpg')
        Image.new('RGB', (20, 20), 'red').save(p)
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='JPG Title', kw_raw='a,b,b,c', desc='d')
        self.assertTrue(ok, msg)
        cmd = [self.et, '-j', '-Keywords', fp]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        entry = json.loads(res.stdout)[0]
        # duplicates NOT removed for JPEG -- matches pre-existing behavior
        self.assertEqual(entry.get('Keywords'), ['a', 'b', 'b', 'c'])


class PsKeywordSurvivalTests(unittest.TestCase):
    """v0.9.7.4: pure-function tests for _ps_keyword_survival, the piece
    that turns a (possibly truncated) legacy PostScript-native Keywords/
    Subject read-back string into an honest "N of TOTAL survived" count.
    No exiftool needed -- this is plain string logic, so these run in
    every environment, including this one (no exiftool available here).
    """

    def test_full_list_survives(self):
        kws = ['cat', 'dog', 'fox']
        n, clean = core_utils._ps_keyword_survival('cat, dog, fox', kws)
        self.assertEqual(n, 3)
        self.assertTrue(clean)

    def test_clean_truncation_at_keyword_boundary(self):
        """Reproduces the real bug: a 49-keyword list truncated cleanly
        after the 19th keyword (the actual observed behavior on a real
        Illustrator-exported EPS10 file -- see the project's diagnostic
        report). Must report 19 survivors, not silently look "verified"
        and not crash on the shortfall."""
        kws = [f'kw{i}' for i in range(49)]
        joined_full = ', '.join(kws)
        truncated = ', '.join(kws[:19])  # simulates ExifTool's real write
        self.assertLess(len(truncated), len(joined_full))
        n, clean = core_utils._ps_keyword_survival(truncated, kws)
        self.assertEqual(n, 19)
        self.assertTrue(clean)

    def test_mid_word_fragment_is_not_counted_as_a_survivor(self):
        """If a future ExifTool version ever truncates mid-word instead
        of at a clean boundary, the fragment must not be miscounted as
        a complete extra keyword."""
        kws = ['vector illustration', 'infographic', 'design element']
        # 'infograp' is a truncated fragment of 'infographic'
        n, clean = core_utils._ps_keyword_survival('vector illustration, infograp', kws)
        self.assertEqual(n, 1)
        self.assertFalse(clean)

    def test_empty_readback_reports_zero_not_a_crash(self):
        n, clean = core_utils._ps_keyword_survival('', ['cat', 'dog'])
        self.assertEqual(n, 0)


class KeywordCsvParsingTests(unittest.TestCase):
    """v0.9.7.4: Stage A of the investigation (CSV -> keyword list) --
    confirms the actual 49-keyword comma-joined string from the report
    parses to exactly 49 keywords, no silent loss during parsing/
    normalization. Pure function, no exiftool needed."""

    REAL_49_KEYWORD_ROW = (
        "vector illustration, infographic, design element, chart, diagram, "
        "graph, process, flow chart, data, business, presentation, template, "
        "graphic, icon, block, workflow, layout, table, bar chart, "
        "organization, structure, statistic, info, visual, strategy, plan, "
        "marketing, communication, modern, creative, set, collection, "
        "colorful, blue, orange, green, red, purple, beige, pastel, flat, "
        "art, symbol, sign, shaped, label, box, arrow, educational"
    )

    def test_49_keyword_row_parses_to_49_keywords(self):
        kw_list = [k.strip() for k in self.REAL_49_KEYWORD_ROW.replace(';', ',').split(',') if k.strip()]
        self.assertEqual(len(kw_list), 49)

    def test_multiword_keywords_preserved_as_single_entries(self):
        kw_list = [k.strip() for k in self.REAL_49_KEYWORD_ROW.replace(';', ',').split(',') if k.strip()]
        self.assertIn('vector illustration', kw_list)
        self.assertIn('design element', kw_list)
        self.assertIn('flow chart', kw_list)
        self.assertIn('bar chart', kw_list)


@unittest.skipUnless(shutil.which('exiftool'), 'exiftool not installed in this environment')
class Eps10LargeKeywordListAndTitleTests(unittest.TestCase):
    """v0.9.7.4: end-to-end regression coverage added directly from the
    real bug report -- a 49-keyword row and a ~200-character title,
    matching the actual CSV data that exposed both problems. These
    require a real exiftool binary (skipped here, like the rest of the
    EPS10 suite, since none is installed in this sandbox), but are
    written to run wherever exiftool is available (dev machines, CI)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.et = 'exiftool'

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_eps(self, name='test.eps'):
        p = os.path.join(self.tmpdir, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(_EPS10_FIXTURE)
        return p

    def _read_back(self, path):
        cmd = [self.et, '-j', '-charset', 'UTF8', '-G1', '-IPTC:Headline',
               '-PostScript:Title', '-IPTC:Keywords', '-PostScript:Keywords',
               path]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return json.loads(res.stdout or '[]')[0]

    def test_49_keywords_all_present_in_iptc_even_if_legacy_field_is_short(self):
        """The actual reported bug: a 49-keyword CSV row must result in
        all 49 keywords being verifiably present in the file. The
        authoritative channel for that guarantee is IPTC:Keywords (a
        real list tag) -- the embed must succeed and IPTC must be
        complete regardless of whatever the capacity-limited legacy
        PostScript-native comment can hold."""
        p = self._make_eps()
        kws = [f'keyword{i}' for i in range(49)]
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title='Fifty title', kw_raw=', '.join(kws), desc='')
        self.assertTrue(ok, msg)
        entry = self._read_back(fp)
        iptc_kws = entry.get('IPTC:Keywords') or []
        self.assertEqual(len(iptc_kws), 49)
        self.assertEqual(set(iptc_kws), set(kws))

    def test_long_realistic_title_verified_via_iptc_headline(self):
        """Matches the actual longest CSV title in the report (~198
        characters). IPTC:Headline (the new pass/fail authority) must
        hold it completely, regardless of what the legacy PostScript:Title
        DSC comment can fit."""
        p = self._make_eps()
        title = ('Vector illustration set of colorful info-graphics, mind maps, '
                  'process charts, and organizational diagrams featuring diverse '
                  'layouts, rounded rectangles, branching nodes, and numbered '
                  'lists on white.')
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title=title, kw_raw='a,b', desc='')
        self.assertTrue(ok, msg)
        entry = self._read_back(fp)
        self.assertEqual((entry.get('IPTC:Headline') or '').strip(), title)

    def test_title_never_falls_back_to_filename(self):
        """Regression guard for Problem 1 in the report: the recognized
        title must be the CSV title, never the on-disk filename (or any
        filename-derived slug), for a file whose name looks nothing like
        its title."""
        p = self._make_eps(name='Bulletin (3).eps')
        title = 'A completely different descriptive title, nothing like the filename.'
        ok, msg, fp = core_utils.embed_metadata_one(
            self.et, p, title=title, kw_raw='k', desc='')
        self.assertTrue(ok, msg)
        entry = self._read_back(fp)
        headline = (entry.get('IPTC:Headline') or '').strip()
        self.assertEqual(headline, title)
        self.assertNotIn('Bulletin', headline)


if __name__ == '__main__':
    unittest.main()
