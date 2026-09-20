"""
v0.9.9 regression test: Ghostscript/ffmpeg/ffprobe subprocess calls
must always pass creationflags=CREATE_NO_WINDOW (on win32), matching
the pattern core/utils.py already uses for every ExifTool call.

Without this, a --windowed/frozen build pops a real OS console window
for every EPS import, every video import, and every Generate click on
a vector or video file -- see CHANGELOG.md v0.9.9 for the full bug
writeup. This test exists so a future edit to any of these three
files can't silently drop the flag again without a red test.

Plain stdlib unittest, no extra dependency, matching test_regression.py.
Run from the backend/ directory:
    python3 -m unittest tests.test_subprocess_no_window -v
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class _FakeCompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess -- just enough
    shape for each function under test to read returncode/stdout/stderr
    without touching a real gs/ffmpeg/ffprobe binary."""
    def __init__(self, stdout=b""):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = b""


class _FakePopen:
    """v0.9.6.5: stand-in for subprocess.Popen, for the two call sites
    (render_via_ghostscript, extract_thumbnail_frame) that were moved
    from subprocess.run() to core.cancellable_subprocess.run_cancellable()
    in v0.9.6 (see that module's docstring -- lets a stale Clear-All/
    newer-import actually terminate a slow Ghostscript/ffmpeg child
    instead of only being checked between files). run_cancellable()
    calls .communicate(timeout=POLL_SEC) in a loop and expects either a
    real (stdout, stderr) tuple back or a subprocess.TimeoutExpired --
    this always returns immediately, same one-shot-success shape
    _FakeCompletedProcess gave the old direct subprocess.run() mocks."""
    def __init__(self, *args, **kwargs):
        self.returncode = 0
        self.args = args
        self.kwargs = kwargs

    def communicate(self, timeout=None):
        return (b"", b"")

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        pass


_FAKE_FFPROBE_JSON = (
    b'{"format": {"duration": "1.0", "size": "100"}, '
    b'"streams": [{"codec_type": "video", "width": 10, "height": 10, '
    b'"r_frame_rate": "30/1", "codec_name": "h264"}]}'
)


class GhostscriptNoWindowTests(unittest.TestCase):
    def test_render_via_ghostscript_passes_creationflags(self):
        import vector.renderer as renderer
        import core.cancellable_subprocess as cancellable
        popen_mock = MagicMock(side_effect=_FakePopen)
        with patch.object(renderer, "_find_gs", return_value="/usr/bin/gs"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=100), \
             patch.object(cancellable.subprocess, "Popen", popen_mock):
            renderer.render_via_ghostscript("/tmp/x.eps", out_path="/tmp/out.png")
        self.assertIn("creationflags", popen_mock.call_args.kwargs,
                       "render_via_ghostscript (via run_cancellable) must pass "
                       "creationflags so it never pops a console window on Windows")


class FfprobeNoWindowTests(unittest.TestCase):
    def test_analyze_video_technical_passes_creationflags(self):
        import video.technical as technical
        with patch.object(technical, "_ffprobe_path", return_value="/usr/bin/ffprobe"), \
             patch.object(technical.subprocess, "run",
                           return_value=_FakeCompletedProcess(_FAKE_FFPROBE_JSON)) as run:
            technical.analyze_video_technical("/tmp/x.mp4")
        self.assertIn("creationflags", run.call_args.kwargs,
                       "analyze_video_technical must pass creationflags "
                       "so ffprobe never pops a console window on Windows")


class FfmpegNoWindowTests(unittest.TestCase):
    def test_extract_thumbnail_frame_passes_creationflags(self):
        import video.frames as frames
        import core.cancellable_subprocess as cancellable
        popen_mock = MagicMock(side_effect=_FakePopen)
        with patch.object(frames, "_ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=100), \
             patch.object(cancellable.subprocess, "Popen", popen_mock):
            frames.extract_thumbnail_frame("/tmp/x.mp4", out_path="/tmp/out.png")
        self.assertIn("creationflags", popen_mock.call_args.kwargs,
                       "extract_thumbnail_frame (via run_cancellable) must pass "
                       "creationflags so ffmpeg never pops a console window on Windows")

    def test_extract_thumbnail_frame_scales_in_ffmpeg(self):
        """v0.9.6.5 ROOT-CAUSE FIX: extract_thumbnail_frame must ask
        ffmpeg itself to scale the frame down (-vf scale=...), not
        extract at full source resolution and rely on a later PIL
        resize -- that used to mean decoding+writing a full 4K frame
        just to throw away >99% of the pixels a moment later."""
        import video.frames as frames
        import core.cancellable_subprocess as cancellable
        popen_mock = MagicMock(side_effect=_FakePopen)
        with patch.object(frames, "_ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=100), \
             patch.object(cancellable.subprocess, "Popen", popen_mock):
            frames.extract_thumbnail_frame("/tmp/x.mp4", out_path="/tmp/out.png")
        cmd = popen_mock.call_args.args[0]
        self.assertIn("-vf", cmd, "extract_thumbnail_frame must pass an ffmpeg -vf "
                                   "scale filter so ffmpeg itself bounds the frame size")
        vf_value = cmd[cmd.index("-vf") + 1]
        self.assertIn("scale", vf_value)

    def test_extract_frames_fixed_interval_passes_creationflags(self):
        import video.frames as frames
        with patch.object(frames, "_ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
             patch("os.path.exists", return_value=True), \
             patch.object(frames.subprocess, "run",
                           return_value=_FakeCompletedProcess()) as run:
            frames.extract_frames_fixed_interval("/tmp/x.mp4", duration_sec=2.0,
                                                   count=1, out_dir="/tmp")
        self.assertIn("creationflags", run.call_args.kwargs,
                       "extract_frames_fixed_interval must pass creationflags "
                       "so ffmpeg never pops a console window on Windows")

    def test_extract_frames_fixed_interval_still_full_resolution(self):
        """v0.9.6.5: the AI-analysis frame path (extract_frames_fixed_
        interval) must NOT gain a -vf scale filter -- that's explicitly
        the batch's own instruction ('video AI frame analysis' must
        stay untouched). Only extract_thumbnail_frame (the UI-thumbnail
        path, tested above) should scale in ffmpeg."""
        import video.frames as frames
        with patch.object(frames, "_ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
             patch("os.path.exists", return_value=True), \
             patch.object(frames.subprocess, "run",
                           return_value=_FakeCompletedProcess()) as run:
            frames.extract_frames_fixed_interval("/tmp/x.mp4", duration_sec=2.0,
                                                   count=1, out_dir="/tmp")
        for call in run.call_args_list:
            cmd = call.args[0]
            self.assertNotIn("-vf", cmd,
                              "extract_frames_fixed_interval (AI analysis path) "
                              "must stay at full resolution, unchanged")

    def test_detect_scenes_passes_creationflags(self):
        import video.frames as frames
        with patch.object(frames, "_ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
             patch.object(frames.subprocess, "run",
                           return_value=_FakeCompletedProcess()) as run:
            with self.assertRaises(ValueError):
                # No scene boundaries in the fake stderr -> falls back to
                # one whole-video scene -> calls extract_frames_fixed_interval
                # -> that also calls subprocess.run (mocked, returns no real
                # frame file) -> raises "No usable scenes". We only care
                # that every subprocess.run call along the way got
                # creationflags, not that this particular path succeeds.
                frames.detect_scenes("/tmp/x.mp4", duration_sec=2.0)
        for call in run.call_args_list:
            self.assertIn("creationflags", call.kwargs,
                           "every ffmpeg call inside detect_scenes must "
                           "pass creationflags")


if __name__ == "__main__":
    unittest.main()
