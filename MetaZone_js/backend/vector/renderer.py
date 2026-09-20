"""Renders vector files to real PNG previews so the existing AI vision
engine (engine/ai_providers.py) can look at them -- every provider we
support takes an image, not a vector file, so this step is mandatory,
not optional polish. This module never fabricates a preview; if
rendering fails, it raises rather than returning a blank/placeholder
image that would silently feed a meaningless picture to the AI.

SVG rendering uses PyMuPDF (already a hard dependency for the
PDF-compatible .ai path), NOT cairosvg. This was a deliberate switch:
cairosvg needs the native Cairo C library at runtime (libcairo-2.dll on
Windows), which isn't a Python package pip can install -- it requires
bundling a chain of GTK-adjacent DLLs, a genuinely fragile, poorly
documented deployment path with many long-standing open issues.
PyMuPDF statically bundles its own renderer inside its wheel, so it
has zero equivalent native-library-discovery problem, and it was
already proven working in this project's CI.

KNOWN TRADEOFF: MuPDF's SVG renderer does not render <linearGradient>/
<radialGradient> fills -- they come out solid black in the preview
image. This does NOT affect the real structural data (gradient count
and real stop colors are still read correctly by svg_structure.py and
injected into the AI prompt as text facts either way) -- it only means
the AI's VISUAL read of a gradient-filled shape won't see the actual
gradient colors, just a black shape. Acceptable tradeoff versus
shipping a Cairo DLL chain that can fail to load on a stock Windows
machine with no clear error message.
"""
import os
import subprocess
import sys
import tempfile

import pymupdf

from core.bin_finder import bundled_pkg_root
from core.cancellable_subprocess import run_cancellable, StaleWorkError


def render_svg_to_png(svg_path, out_path=None, resolution=1024):
    """Real rasterization via PyMuPDF/MuPDF's own SVG renderer (renders
    the actual SVG paint tree, not a guess at what it might look like).
    See the module docstring for the one known limitation (gradients
    render as solid black). Returns the output path."""
    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
    try:
        doc = pymupdf.open(svg_path)
        try:
            page = doc[0]
            zoom = resolution / max(page.rect.width, page.rect.height, 1)
            mat = pymupdf.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(out_path)
        finally:
            doc.close()
    except Exception as e:
        raise ValueError(f"Could not render this SVG for preview: {e}")
    return out_path


def _eps_bbox_points(vector_path):
    """Parses the real %%BoundingBox comment (present in essentially
    every valid EPS/legacy-.ai file's header -- it's required by the
    EPS spec) to get the page's actual size in points, WITHOUT asking
    Ghostscript to render anything. Returns (width_pt, height_pt) or
    None if no usable bbox line is found in the first 8KB (some files
    use %%BoundingBox: (atend), meaning the real numbers are written
    at the end of the file after the content -- not worth a full-file
    scan just for a thumbnail-sizing hint; the DPI clamp in
    render_via_ghostscript's caller covers this fallback case)."""
    try:
        with open(vector_path, "rb") as f:
            head = f.read(8192)
        for line in head.split(b"\n"):
            if line.startswith(b"%%BoundingBox:"):
                parts = line.split(b":", 1)[1].split()
                if len(parts) == 4:
                    llx, lly, urx, ury = (float(p) for p in parts)
                    w, h = urx - llx, ury - lly
                    if w > 0 and h > 0:
                        return w, h
    except (OSError, ValueError):
        pass
    return None


def dpi_for_target_px(vector_path, target_px, default_dpi, min_dpi=1, max_dpi=600):
    """Public, general form of the same page-size-aware DPI computation
    used by render_vector_thumbnail -- reads the file's real
    %%BoundingBox and picks a DPI that lands close to target_px on the
    longest edge, instead of a caller-chosen flat DPI blowing up for a
    physically large page. Shared by both the 160px import-thumbnail
    target and (see vector/analyzer.py) the higher-resolution
    AI-analysis preview target."""
    bbox = _eps_bbox_points(vector_path)
    if not bbox:
        return default_dpi
    w_pt, h_pt = bbox
    dpi = 72.0 * target_px / max(w_pt, h_pt)
    return max(min_dpi, min(max_dpi, dpi))


def _thumb_dpi_for(vector_path, target_px=200, default_dpi=36, min_dpi=1, max_dpi=150):
    """Import-grid-thumbnail-specific wrapper around dpi_for_target_px
    (target_px=200, well above the 160px card size so JPEG re-encoding
    in make_thumb_b64 always downscales rather than upscales)."""
    return dpi_for_target_px(vector_path, target_px, default_dpi, min_dpi, max_dpi)


def render_via_ghostscript(vector_path, out_path=None, resolution=200, is_stale=None):
    """Real rasterization for PostScript-family files (.eps, legacy
    .ai) via Ghostscript -- the same real renderer Adobe/print
    pipelines use to interpret PostScript, not a custom guesser.
    Requires the `gs` binary to be installed on the system (same
    external-dependency pattern MetaZone already uses for exiftool),
    OR the bundled copy shipped in a Windows build's gs_pkg/ folder
    (see core/bin_finder.py + .github/workflows/build_js.yml).
    """
    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)

    gs_bin = _find_gs()
    if not gs_bin:
        raise RuntimeError(
            "Ghostscript ('gs') is not installed. It's required to preview "
            ".eps and legacy .ai files. Install it from ghostscript.com/releases "
            "or via your package manager (e.g. 'apt install ghostscript', "
            "'brew install ghostscript')."
        )

    cmd = [
        gs_bin, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dEPSCrop",
        "-sDEVICE=png16m", f"-r{resolution}",
    ]
    # v0.9.8: when running the bundled copy from a frozen Windows
    # build, tell Ghostscript exactly where its own Resource/lib/
    # iccprofiles folders are via -I / -sICCProfilesDir, instead of
    # relying on its own relative-to-exe auto-detection. That
    # auto-detection assumes gswin64c.exe sits in a conventional
    # <installdir>/bin/ layout -- true here (gs_pkg/bin/), but explicit
    # is safer than implicit for a bundle this bespoke, and this has
    # not been verified against a real Windows install of the bundled
    # build (no Windows machine available in this sandbox -- see
    # CHANGELOG). A system-installed `gs` on PATH (dev machines, Linux,
    # macOS) needs none of this -- it already knows where it lives.
    gs_pkg_root = bundled_pkg_root("gs_pkg")
    if gs_pkg_root:
        resource_dir = os.path.join(gs_pkg_root, "Resource")
        lib_dir = os.path.join(gs_pkg_root, "lib")
        icc_dir = os.path.join(gs_pkg_root, "iccprofiles")
        if os.path.isdir(resource_dir):
            cmd.append(f"-I{resource_dir}")
        if os.path.isdir(lib_dir):
            cmd.append(f"-I{lib_dir}")
        if os.path.isdir(icc_dir):
            cmd.append(f"-sICCProfilesDir={icc_dir}")
    cmd += [f"-sOutputFile={out_path}", vector_path]
    # v0.9.9: same CREATE_NO_WINDOW pattern core/utils.py already uses
    # for every ExifTool call -- without it, a --windowed/frozen build
    # (no console of its own) causes Windows to allocate and briefly
    # show a brand-new console window for this child process on every
    # single EPS/legacy-.ai import and every Generate click. This was
    # missed when Ghostscript rendering was added; it never inherited
    # the ExifTool call's existing no-window fix.
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    # v0.9.6: run_cancellable (not subprocess.run) so a stale
    # is_stale() (Clear All / a newer import superseding this one)
    # actually terminates this Ghostscript process instead of the
    # caller being stuck waiting out the full 30s timeout -- see
    # core/cancellable_subprocess.py's module docstring for the root
    # cause this fixes.
    try:
        result = run_cancellable(cmd, timeout=30, is_stale=is_stale, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise ValueError("Rendering this file timed out — it may be corrupted or unusually complex.")
    except StaleWorkError:
        raise
    if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        err = result.stderr.decode(errors="replace")[:300]
        raise ValueError(f"Ghostscript could not render this file: {err}")
    return out_path


def _find_gs():
    from core.bin_finder import find_bundled_binary
    return find_bundled_binary("gs_pkg/bin", ["gswin64c.exe", "gswin32c.exe", "gs.exe", "gs"])


def render_vector_thumbnail(vector_path, out_path=None, is_stale=None):
    """Lightweight real preview for the import grid's card (as opposed
    to detect_and_analyze's full render, which is sized for the AI
    vision call). Dispatches to the same real renderers by real
    detected format -- never a guess based on extension alone -- just
    at a low resolution, since this only needs to be legible at
    card-thumbnail size. Raises on failure; the caller (session.
    make_thumb_b64) decides the graceful fallback (a file-type
    placeholder) rather than this module ever faking a preview.

    v0.9.6: `is_stale` (optional zero-arg callable) is threaded through
    to render_via_ghostscript so a Clear-All mid-render actually kills
    the Ghostscript child instead of just being ignored until it
    finishes on its own -- see core/cancellable_subprocess.py. SVG/PDF-
    compatible-.ai rendering (PyMuPDF, in-process) has no subprocess to
    cancel; those stay fast enough (bounded by `resolution`, not a
    child process) that this isn't the runaway-thread risk the
    Ghostscript path is.
    """
    import os as _os
    from vector.ai_pdf_structure import is_pdf_compatible, render_ai_pdf_preview

    ext = _os.path.splitext(vector_path)[1].lower()
    if ext == ".svg":
        return render_svg_to_png(vector_path, out_path=out_path, resolution=160)
    if ext == ".ai" and is_pdf_compatible(vector_path):
        if out_path is None:
            fd, out_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
        return render_ai_pdf_preview(vector_path, out_path, resolution=160)
    # Legacy .ai and true .eps: Ghostscript. v0.9.6: DPI is now
    # computed from the page's real %%BoundingBox (see
    # _thumb_dpi_for's docstring) so a physically large page still
    # renders close to a 200px-longest-edge bitmap instead of a fixed
    # low DPI still producing a multi-thousand-pixel image for a
    # multi-thousand-point page.
    dpi = _thumb_dpi_for(vector_path)
    return render_via_ghostscript(vector_path, out_path=out_path, resolution=round(dpi), is_stale=is_stale)
