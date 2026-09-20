"""v0.9.6 perf fix: cancellable subprocess runner shared by
vector/renderer.py and video/frames.py.

ROOT CAUSE (see project's bug report, item 5): Clear All bumps
Session.import_epoch, which _prefetch_thumbs already checks *between*
per-file units of work -- but the actual Ghostscript/ffmpeg
subprocess.run() call for the file that's currently rendering when
Clear is pressed used to run to completion (or its fixed 20-30s
timeout) no matter what, because subprocess.run() blocks until the
child exits and has no way to observe import_epoch changing out from
under it. On a large/slow EPS or a big 4K video, that's a real,
user-visible number of seconds where Clear "did nothing" from the
user's point of view.

This runs the child with Popen instead of run(), and polls it in short
slices (POLL_SEC) so the caller-supplied `is_stale()` check can act
inside a single subprocess call, not just between them. If the work
goes stale mid-render, the child is terminated (and killed if it
doesn't exit quickly) and StaleWorkError is raised -- callers already
have to handle "rendering failed" (see render_vector_thumbnail's
try/except in session.make_thumb_b64), so this reuses that same
graceful-fallback path rather than introducing a new one.

This does not make Ghostscript/ffmpeg "interruptible" mid-frame the
way a native cancel would -- it's still a terminate() on an OS
process, same mechanism a user hitting Ctrl+C would trigger -- but
that's a real, bounded stop (POLL_SEC granularity) instead of "wait up
to 30s no matter what," which is the actual reported symptom.
"""
import subprocess
import time


class StaleWorkError(Exception):
    """Raised when is_stale() became true while a subprocess was
    still running -- the caller's epoch moved on (Clear All / a new
    import superseded this one) and the result must not be used."""


POLL_SEC = 0.15


def run_cancellable(cmd, timeout=30, is_stale=None, creationflags=0):
    """Same contract as subprocess.run(cmd, capture_output=True,
    timeout=timeout, creationflags=creationflags), except:
    - if is_stale() (called between polls, never more than ~POLL_SEC
      after the caller became stale) returns True, the child is
      terminated/killed and StaleWorkError is raised instead of
      returning a result nobody wants anymore.
    - is_stale=None behaves like a plain subprocess.run() (no polling
      overhead beyond POLL_SEC-grained waits, functionally identical
      timeout behavior).
    Returns an object with .returncode/.stdout/.stderr, same shape as
    subprocess.run's CompletedProcess, so existing call sites that do
    `result.returncode`/`result.stderr` need no changes beyond passing
    is_stale through.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    start = time.monotonic()
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=POLL_SEC)
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pass
            if is_stale is not None and is_stale():
                _kill(proc)
                raise StaleWorkError("Superseded by Clear All / a newer import.")
            if time.monotonic() - start > timeout:
                _kill(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)
    except BaseException:
        # Any other unexpected exception (including KeyboardInterrupt
        # during dev/testing) must not leave an orphaned child process
        # behind -- best-effort cleanup, then re-raise unchanged.
        if proc.poll() is None:
            _kill(proc)
        raise


def _kill(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except Exception:
        pass
