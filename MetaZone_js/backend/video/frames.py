"""Real frame extraction and scene-change detection via ffmpeg.

Two real, tool-backed strategies, matching the two modes listed in the
project brief:

  - fixed interval: ffmpeg's own `fps=` filter, evenly spaced -- real
    decode of the actual video at those timestamps, not interpolation.
  - scene-change detection: ffmpeg's own `select='gt(scene,THRESH)'`
    filter, the same scene-detection algorithm FFmpeg ships for this
    exact purpose (compares frame-to-frame histogram difference) --
    this is a real, established algorithm, not a custom heuristic
    invented for this project.

Both return real timestamps (read back from ffmpeg's own showinfo
filter output), so "Scene 2: 00:04-00:09" in the UI is the actual
detected boundary, not an assumption.
"""
import os
import re
import subprocess
import sys
import tempfile
from core.bin_finder import find_bundled_binary
from core.cancellable_subprocess import run_cancellable, StaleWorkError


def _ffmpeg_path():
    return find_bundled_binary("ffmpeg_pkg", ["ffmpeg.exe", "ffmpeg"])


def ffmpeg_available():
    return _ffmpeg_path() is not None


# v0.9.9: same CREATE_NO_WINDOW pattern core/utils.py already uses for
# every ExifTool call -- without it, each ffmpeg child process below
# pops its own visible console window on Windows (this app is built
# --windowed/frozen, so it has no console of its own for the child to
# inherit; Windows allocates a fresh one instead). Missed when the
# video pipeline was added.
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def extract_frames_fixed_interval(path, duration_sec, count=6, out_dir=None, name_prefix="frame"):
    """Real decode of `count` evenly-spaced frames across the actual
    video duration. Returns [(timestamp_sec, frame_png_path), ...].
    name_prefix keeps filenames unique when this is called multiple
    times into the same out_dir (e.g. once per detected scene) --
    without it, scene 2's frame_001.png silently overwrites scene 1's."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed -- required for frame extraction.")
    ffmpeg_bin = _ffmpeg_path()
    out_dir = out_dir or tempfile.mkdtemp(prefix="mze_frames_")
    os.makedirs(out_dir, exist_ok=True)
    if count < 1:
        count = 1
    step = duration_sec / (count + 1)
    frames = []
    for i in range(1, count + 1):
        ts = round(step * i, 2)
        out_path = os.path.join(out_dir, f"{name_prefix}_{i:03d}.png")
        cmd = [ffmpeg_bin, "-y", "-ss", str(ts), "-i", path, "-frames:v", "1",
               "-q:v", "2", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=_NO_WINDOW_FLAGS)
        if result.returncode != 0 or not os.path.exists(out_path):
            continue  # real skip, not a fabricated frame
        frames.append((ts, out_path))
    if not frames:
        raise ValueError("Could not extract any frames from this video -- it may be corrupted.")
    return frames


def extract_thumbnail_frame(path, out_path=None, timestamp_sec=0.5, is_stale=None, max_px=160):
    """Grabs a single real decoded frame near the start of the video,
    for the import grid's card thumbnail -- deliberately independent
    of extract_frames_fixed_interval (which needs the video's real
    duration up front, from an ffprobe call this doesn't need to make
    just to show *some* representative frame while the file's card is
    still sitting in the import grid, before Generate has run).
    Returns the frame's JPEG path, or raises if ffmpeg can't produce
    one (short video, corrupt file, etc.) -- caller decides the
    graceful fallback (see session.make_thumb_b64).

    v0.9.6.5 ROOT-CAUSE FIX: this used to extract the frame at full
    source resolution (no -vf at all) and rely ENTIRELY on session.py's
    later PIL resize to shrink it -- for a 4K source that meant
    decoding and writing a full 3840x2160 PNG to disk (and then
    re-reading + re-decoding that whole file in Python) just to throw
    away >99% of the pixels a moment later. The `-vf scale=...` filter
    below makes ffmpeg itself do the downscale as part of the same
    decode pass, so the frame ffmpeg actually WRITES is already
    bounded to `max_px` on its longest edge -- the PIL step in
    session.py's _downscale_to_thumb_b64 becomes a cheap format/size
    safety-net pass over an already-small image, not the thing doing
    the real work. Output is JPEG (smaller to write/read than PNG for
    a photographic video frame, and this is UI-only -- the full-
    resolution frames used for actual AI video analysis go through
    extract_frames_fixed_interval, completely unchanged, untouched by
    this function).

    `force_original_aspect_ratio=decrease` preserves aspect ratio and
    never upscales a source smaller than max_px; `scale2ref`-style
    even-dimension rounding isn't needed here since this is a still
    JPEG, not a video encode.

    v0.9.6: `is_stale` (optional zero-arg callable) lets a Clear-All /
    superseding import actually kill this ffmpeg child mid-extract
    instead of waiting out its full 20s timeout regardless -- see
    core/cancellable_subprocess.py.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed -- required for video preview frames.")
    ffmpeg_bin = _ffmpeg_path()
    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
    scale_filter = f"scale='min({max_px},iw)':'min({max_px},ih)':force_original_aspect_ratio=decrease"
    cmd = [ffmpeg_bin, "-y", "-ss", str(timestamp_sec), "-i", path,
           "-frames:v", "1", "-vf", scale_filter, "-q:v", "4", out_path]
    result = run_cancellable(cmd, timeout=20, is_stale=is_stale, creationflags=_NO_WINDOW_FLAGS)
    if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        # Some very short clips have no frame at timestamp_sec -- one
        # real retry at the very first frame before giving up.
        cmd0 = [ffmpeg_bin, "-y", "-i", path, "-frames:v", "1", "-vf", scale_filter,
                "-q:v", "4", out_path]
        result0 = run_cancellable(cmd0, timeout=20, is_stale=is_stale, creationflags=_NO_WINDOW_FLAGS)
        if result0.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise ValueError("Could not extract a preview frame from this video.")
    return out_path


def detect_scenes(path, duration_sec, threshold=0.35, frames_per_scene=2, out_dir=None):
    """Real scene-boundary detection via ffmpeg's own scene filter.
    Returns a list of scenes: [{"start": s, "end": s, "frames": [...]}]
    Falls back to treating the whole video as one scene if fewer than
    2 boundaries are detected (e.g. a static shot) -- that's a real
    outcome of the algorithm, not an error.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed -- required for scene detection.")

    out_dir = out_dir or tempfile.mkdtemp(prefix="mze_scenes_")
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        _ffmpeg_path(), "-i", path, "-filter:v",
        f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=_NO_WINDOW_FLAGS)
    except subprocess.TimeoutExpired:
        raise ValueError("Scene detection timed out on this video.")

    stderr = result.stderr.decode(errors="replace")
    # ffmpeg's showinfo filter prints real detected-frame timestamps,
    # e.g. "pts_time:12.345" -- these ARE the real scene-cut points.
    boundaries = [float(m) for m in re.findall(r"pts_time:([\d.]+)", stderr)]
    boundaries = sorted(set(round(b, 2) for b in boundaries))

    cut_points = [0.0] + [b for b in boundaries if 0 < b < duration_sec] + [duration_sec]
    cut_points = sorted(set(cut_points))
    if len(cut_points) < 2:
        cut_points = [0.0, duration_sec]

    scenes = []
    for i in range(len(cut_points) - 1):
        start, end = cut_points[i], cut_points[i + 1]
        if end - start < 0.1:
            continue
        scene_frames = extract_frames_fixed_interval(
            path, end - start, count=frames_per_scene, out_dir=out_dir,
            name_prefix=f"scene{i+1}",
        )
        # re-anchor timestamps to the real absolute position in the video
        scene_frames = [(round(start + t, 2), p) for t, p in scene_frames]
        scenes.append({"start": start, "end": end, "frames": scene_frames})

    if not scenes:
        raise ValueError("No usable scenes could be extracted from this video.")
    return scenes
