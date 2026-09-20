"""Real (but intentionally limited) reading of legacy .ai and true
.eps files -- both are raw PostScript, a full programming language,
not a declarative shape list like SVG or PDF. There is no reliable way
to count "paths" or "shapes" in arbitrary PostScript without actually
executing it as a program, and even then the result is pixels, not
structured geometry.

Rather than faking path/shape/color counts the way a less careful tool
might, this module only reports what's ACTUALLY, RELIABLY present:
the file's own DSC (Document Structuring Conventions) header comments,
which PostScript/EPS files are required to carry for exactly this kind
of tooling. Anything not in this list is genuinely not available for
this file type -- the UI should say so, not guess.
"""
import os
import re
import struct

# "DOS EPS" / "EPS binary" wrapper: a 30-byte binary header some EPS
# exporters (Illustrator on Windows in particular, when the file is
# saved with a bitmap preview attached) prepend in front of the actual
# PostScript stream, so the real '%!PS' text does not start at byte 0
# of the file the way it does for a plain-text EPS. The wrapper is
# just an index telling a consumer where the real PS text lives (plus,
# optionally, where a WMF/TIFF screen-preview image lives) -- the
# PostScript content itself, and therefore the artwork, is untouched
# and still fully present in the file. See the EPS spec's "Special
# Files" appendix ("Encapsulated PostScript Interchange Format").
_DOS_EPS_MAGIC = b"\xc5\xd0\xd3\xc6"


def _dos_eps_ps_offset_and_length(head_bytes, file_size):
    """If head_bytes starts with the DOS-EPS binary-header magic
    number, returns (ps_offset, ps_length) for the real PostScript
    stream inside the file. Returns None if the magic number isn't
    present, or if the header's own offsets don't make sense for this
    file (genuinely corrupt wrapper, not just "unfamiliar format)."""
    if len(head_bytes) < 30 or head_bytes[:4] != _DOS_EPS_MAGIC:
        return None
    ps_off, ps_len = struct.unpack_from("<II", head_bytes, 4)
    if ps_off <= 0 or ps_len <= 0 or ps_off + ps_len > file_size:
        return None
    return ps_off, ps_len


_DSC_FIELDS = {
    "%%BoundingBox:": "bounding_box",
    "%%HiResBoundingBox:": "bounding_box_hires",
    "%%Creator:": "creator",
    "%%CreationDate:": "creation_date",
    "%%Title:": "title",
    "%%For:": "for_user",
    "%%Pages:": "pages",
    "%%DocumentFonts:": "document_fonts",
    "%%DocumentNeededFonts:": "needed_fonts",
    "%%LanguageLevel:": "language_level",
    "%%ColorUsage:": "color_usage",
    "%%DocumentProcessColors:": "process_colors",
    "%%DocumentCustomColors:": "custom_colors",
}


def parse_ps_header(path, max_header_bytes=8192):
    """Reads only the DSC header comments -- real fields the file
    itself declares, read as plain text. Never touches or interprets
    the actual PostScript program body."""
    is_dos_eps = False
    try:
        with open(path, "rb") as f:
            probe = f.read(30)
            file_size = os.fstat(f.fileno()).st_size
            dos_eps_range = _dos_eps_ps_offset_and_length(probe, file_size)
            if dos_eps_range:
                is_dos_eps = True
                ps_off, ps_len = dos_eps_range
                f.seek(ps_off)
                head = f.read(min(max_header_bytes, ps_len)).decode(
                    "latin-1", errors="replace"
                )
            else:
                f.seek(0)
                head = f.read(max_header_bytes).decode("latin-1", errors="replace")
    except OSError as e:
        raise ValueError(f"Could not open the file: {e}")

    if "%!PS" not in head[:32] and "%!Adobe" not in head[:32]:
        if probe[:4] == _DOS_EPS_MAGIC:
            # We recognized the DOS-EPS wrapper itself, but its own
            # offsets don't point at real PostScript text -- this is
            # a genuinely malformed/corrupt wrapper, not a format we
            # simply don't understand.
            raise ValueError(
                "This file has a DOS EPS binary header, but its embedded "
                "PostScript section is missing or corrupt."
            )
        raise ValueError(
            "This doesn't look like a valid PostScript/EPS file "
            "(missing the '%!PS' header)."
        )

    result = {"format": "ps-legacy-dos-binary" if is_dos_eps else "ps-legacy"}
    for line in head.splitlines():
        for prefix, key in _DSC_FIELDS.items():
            if line.startswith(prefix):
                result[key] = line[len(prefix):].strip()

    bbox = result.get("bounding_box")
    if bbox:
        parts = bbox.split()
        if len(parts) == 4:
            try:
                x0, y0, x1, y1 = (float(p) for p in parts)
                result["width_pt"] = round(x1 - x0, 2)
                result["height_pt"] = round(y1 - y0, 2)
            except ValueError:
                pass

    result["structural_analysis_available"] = False
    result["structural_analysis_note"] = (
        "This is a legacy PostScript-based file. Real path/shape/color "
        "counts aren't reliably extractable without a full PostScript "
        "interpreter -- only the header fields above are genuinely "
        "known. Visual AI analysis, when a preview can be rendered, "
        "uses that real rendered preview -- otherwise these header "
        "facts alone are used (see the result's render_error field)."
    )
    return result
