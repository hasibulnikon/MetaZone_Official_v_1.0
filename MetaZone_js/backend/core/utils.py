"""Small stateless helpers: exiftool discovery, file matching, thumbnail
generation, filesize formatting, provider model-id <-> label lookups.
No AI calls beyond image encoding, no app state.
"""
import os, sys, subprocess, base64, socket, time, hashlib, json
# NOTE (JS migration, Stage 1 finding): core/utils.py mixes pure
# business logic (image validation, downscaling, exiftool, file
# indexing) with two CTk-specific functions (make_thumb,
# make_thumb_min_edge, which return ctk.CTkImage) and set_window_icon
# (which takes a Tk window). In the Tkinter app this was harmless
# since ctk was always available; in the JS-frontend backend, ctk
# should not be a dependency at all -- thumbnails become plain <img
# src="data:..."> in the browser, not CTkImage objects.
# Real fix (Stage 4/5, not done here): split this file into
# core/utils.py (pure) and a small ui-only helper retired entirely,
# since CTkImage has no JS-side equivalent -- callers will use PIL
# directly + base64-encode for the <img> tag instead.
# For this Stage-3 shell, the import is made optional so the pure
# functions (used by get_active_keys_summary, etc.) work without
# CustomTkinter installed in the JS-only environment.
try:
    import customtkinter as ctk
except ImportError:
    ctk = None
from PIL import Image
from core.constants import AI_PROVIDERS, VECTOR_EXTS, VIDEO_EXTS

def model_label(provider, model_id):
    for label, mid in AI_PROVIDERS.get(provider, {}).get("models", []):
        if mid == model_id:
            return label
    return model_id.split("/")[-1].split(":")[0][:22]

def model_id_from_label(provider, label):
    for lbl, mid in AI_PROVIDERS.get(provider, {}).get("models", []):
        if lbl == label:
            return mid
    return label

def _app_root():
    # Resolve relative to the app entry point (app.py), not this module —
    # exiftool.exe ships next to app.py / the EXE, not next to core/utils.py.
    if getattr(sys,"frozen",False):
        return os.path.dirname(sys.executable)
    main_mod = sys.modules.get("__main__")
    main_file = getattr(main_mod,"__file__",None)
    return os.path.dirname(os.path.abspath(main_file)) if main_file else os.getcwd()

def get_original_file_meta(path):
    """v0.9.3: original (not thumbnail/downscaled-preview) width/height
    + on-disk file size, for the card grid's new info line under each
    thumbnail. A cheap one-time read done once at import time (see
    Session._prefetch_thumbs), never touched again during generation --
    keeps this separate from prepare_generation_preview's cached
    downscaled copy, which must stay untouched for the AI call itself."""
    size_bytes = 0
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        pass
    width = height = None
    ext = os.path.splitext(path)[1].lower()
    if ext not in VECTOR_EXTS and ext not in VIDEO_EXTS:
        try:
            with Image.open(path) as im:
                width, height = im.size
        except Exception:
            pass
    return {"width": width, "height": height, "size_bytes": size_bytes}

def wait_stable_and_validate_image(path,tries=6,delay_ms=150,recency_window_s=5.0):
    """Confirms a dropped file is actually a complete, readable image
    before the app commits to importing it — but ONLY actually does that
    work for files that could plausibly still be mid-write; an ordinary
    file that's been sitting on disk for a while skips straight through
    with no added cost at all.

    This exists because of a confirmed real bug: dragging an image
    directly from a web browser (rather than from a file already saved
    to disk) can hand the OS drag-and-drop payload a file path before
    the browser has actually finished WRITING that file — browsers that
    support this kind of drag typically do it by writing a temp copy to
    disk on the fly, and that write is not guaranteed to be complete by
    the time the drop event fires. Downloading first and then dragging
    from disk always worked, because a file that's already fully written
    on disk doesn't have this race at all.

    That "already fully written" case is also, by far, the overwhelmingly
    common one — a folder of images that have existed on disk for minutes
    or years, being imported normally. The first version of this check
    ran unconditionally on every single imported file regardless of that,
    which is a confirmed real regression of its own: for a real batch of
    large (e.g. upscaled, tens of megapixels) images that were never at
    any risk of this race at all, doing a multi-hundred-millisecond
    stability poll AND a full Pillow verify() on every one of them,
    sequentially, turned what used to be an instant import into a
    minute-plus wait — benchmarked directly at ~150ms of pure overhead
    per file even on files with no problem whatsoever, which compounds
    fast across a real hundred-plus-file batch.

    The fix: a file's mtime tells us whether it could possibly still be
    mid-write. If it was last modified more than `recency_window_s`
    seconds ago, there is no live write in progress to race against —
    skip straight to an honest "yes, this is fine" with zero extra cost.
    Only a file modified within that recent window (i.e., one that could
    genuinely still be an in-progress browser-drag temp file) gets the
    actual size-stability poll, and even then uses a cheaper open+access
    check rather than a full verify() pass, since the stability poll
    itself is what actually catches the "still being written" case; the
    open check is just confirming Pillow can identify it as an image at
    all, not doing a deep integrity scan.

    Returns (True, None) if it's good, or (False, a short human-readable
    reason) if not — never raises."""
    try:
        mtime=os.path.getmtime(path)
    except Exception:
        return False,"file not found"
    if time.time()-mtime>recency_window_s:
        return True,None  # established file -- no plausible write race, skip the checks entirely
    last_size=-1
    for _ in range(tries):
        try:
            size=os.path.getsize(path)
        except Exception:
            return False,"file not found"
        if size>0 and size==last_size:
            break
        last_size=size
        time.sleep(delay_ms/1000)
    else:
        return False,"file is incomplete (still being written, or 0 bytes)"
    try:
        with Image.open(path) as im:
            im.load()  # forces the header (and, for most formats, first
                       # frame) to actually be read/decoded, without the
                       # deeper structural scan verify() does — enough to
                       # catch a genuinely truncated file from this race,
                       # without paying verify()'s full cost on every file
        return True,None
    except Exception as e:
        return False,f"not a readable image ({e.__class__.__name__})"

def remove_thumb_cache_for(paths,sizes=((100,100),)):
    """Deletes the cached disk thumbnail(s) for specific source files —
    used by Reset-style actions (Prompt-to-Prompt's Image mode reset)
    that want to clean up their own reference-image thumbnails without
    touching the shared thumbnail cache other pages still rely on."""
    cache_dir=_thumb_cache_dir()
    if not cache_dir: return
    for p in paths:
        for size in sizes:
            try:
                key=_thumb_cache_key(p,f"box{size[0]}x{size[1]}")
                fp=os.path.join(cache_dir,key+".jpg")
                if os.path.exists(fp): os.remove(fp)
            except Exception:
                pass

def find_exiftool():
    """Resolve exiftool.exe. Deliberately does NOT fall back to scanning the
    system PATH: a stray/leftover exiftool.exe from some other app's
    PyInstaller temp extraction folder (a _MEIxxxxxx dir) can end up on
    PATH and then vanish once that other process exits, which is exactly
    what produced 'Cannot find file at ...\\_MEIxxxxxx\\...\\exiftool.exe'
    errors here even though the CSV/folder were fine. Only trust paths
    that are actually ours: bundled with this build, or sitting next to
    this app's own exe/script.

    Retries briefly on a miss rather than failing on the very first
    check: right after relaunch_app() restarts the process, Windows/AV
    can still be settling a lock on the freshly re-extracted onefile
    temp folder, which made os.path.exists() report a false negative
    for a file that genuinely is there — showing 'ExifTool missing' in
    red immediately after a theme change even though it wasn't."""
    def _resolve():
        if getattr(sys,'frozen',False):
            b = os.path.join(sys._MEIPASS,'exiftool_pkg','exiftool.exe')
            if os.path.exists(b): return b
        base = _app_root()
        for n in ['exiftool.exe','exiftool']:
            p = os.path.join(base,n)
            if os.path.exists(p): return p
        return None
    found = _resolve()
    if found or not getattr(sys,'frozen',False):
        return found
    for _ in range(4):
        time.sleep(0.25)
        found = _resolve()
        if found:
            return found
    return None

def _icon_paths():
    """Resolve icon.ico/icon.png the same way find_exiftool resolves
    exiftool.exe — bundled next to the frozen EXE (PyInstaller --icon +
    --add-data), or sitting next to app.py in dev."""
    ico = png = None
    if getattr(sys,'frozen',False):
        b = getattr(sys,'_MEIPASS',None)
        if b:
            c = os.path.join(b,'icon.ico')
            if os.path.exists(c): ico = c
            c = os.path.join(b,'icon.png')
            if os.path.exists(c): png = c
    base = _app_root()
    if not ico:
        c = os.path.join(base,'icon.ico')
        if os.path.exists(c): ico = c
    if not png:
        c = os.path.join(base,'icon.png')
        if os.path.exists(c): png = c
    return ico, png

def set_window_icon(window):
    """Applies Meta Zone's real icon to a Tk window's titlebar (and, on
    Windows, the taskbar) instead of the generic Tk/PyInstaller default.
    Safe to call on every window (main app, Embed, API Manager) — quietly
    does nothing if no icon file is bundled rather than raising."""
    ico, png = _icon_paths()
    try:
        if ico and os.name=='nt':
            window.iconbitmap(ico)
            return
    except Exception:
        pass
    if png:
        try:
            from tkinter import PhotoImage
            photo = PhotoImage(file=png)
            window.iconphoto(True, photo)
            window._icon_photo_ref = photo  # keep a reference alive
        except Exception:
            pass

GEN_PREVIEW_MAX_SIDE = 1280

def _gen_preview_cache_dir():
    """Separate from the thumbnail cache — these are full-quality-enough
    JPEGs meant for the AI to actually analyze, not tiny display
    thumbnails, so they're kept in their own subfolder even though both
    live under the same common MetaZone folder."""
    try:
        from core.config import _common_pref_dir
        base = os.path.join(_common_pref_dir(), ".cache", "gen_previews")
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".metazone_cache", "gen_previews")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        return None
    return base

def prepare_generation_preview(path, max_side=GEN_PREVIEW_MAX_SIDE):
    """Resolves the path that should actually be SENT to the AI for
    metadata generation. For a large/high-res original (the 8-15MB
    upscaled-image case this exists for) this returns a cached, already-
    downscaled JPEG instead — same visual content, dramatically smaller
    upload — which is exactly what made generation slow for those files
    in the first place: the network upload and the provider's own image
    processing both scale with file size, not with how much detail an
    AI actually needs to write a title/description/keywords.

    Returns the ORIGINAL path unchanged (never raises, never blocks
    generation) when: the file is a vector/video (no raster preview
    possible), it's already at or under max_side on its long edge (a
    preview would just be a same-size recompress, not worth the extra
    file), or anything about building one fails."""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in VECTOR_EXTS or ext in VIDEO_EXTS:
            return path
        cache_dir = _gen_preview_cache_dir()
        if not cache_dir:
            return path
        key = _thumb_cache_key(path, f"genprev{max_side}")
        cache_path = os.path.join(cache_dir, key + ".jpg")
        if os.path.exists(cache_path):
            return cache_path
        img = Image.open(path)
        w, h = img.size
        if max(w, h) <= max_side:
            return path  # already small enough — original is fine as-is
        img = img.convert("RGB")
        scale = max_side / float(max(w, h))
        new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        img.save(cache_path, "JPEG", quality=90)
        _thumb_cache_cleanup(cache_dir)
        return cache_path
    except Exception:
        return path

def clear_gen_preview_cache():
    """Wipes cached generation previews — same never-touches-prefs.json
    guarantee as clear_thumb_cache, since this only ever looks inside its
    own .cache/gen_previews subfolder."""
    cache_dir = _gen_preview_cache_dir()
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0
    n = 0
    for entry in os.listdir(cache_dir):
        p = os.path.join(cache_dir, entry)
        try:
            if os.path.isfile(p):
                os.remove(p)
                n += 1
        except Exception:
            pass
    return n

def clear_shared_temp_data():
    """v0.8.9: single place every page's Clear All / Reset button calls
    to wipe MetaZone's own on-disk footprint -- the generation-preview
    cache, the thumbnail cache, and the working-CSV export folder used
    for the Meta<->Embed handoff. Previously only session.py's
    Session.clear() (Meta Generator / Image to Prompt Generator) did
    this; P2P's Reset button cleared its own in-memory image list but
    never touched these shared folders. Pressing ANY clear/reset
    control now leaves the app's temp/cache footprint equally clean,
    regardless of which page triggered it -- while each page still
    only clears its OWN in-memory batch/results, never another page's.
    Deliberately never touches prefs.json / stored API keys: those
    live one level up in the common MetaZone folder, never inside any
    of the .cache subfolders this function looks in, and the working-
    CSV folder below is a sibling of those, never the prefs folder
    itself."""
    try:
        clear_gen_preview_cache()
    except Exception:
        pass
    try:
        clear_thumb_cache()
    except Exception:
        pass
    try:
        from core.config import _common_pref_dir
        exports_dir = os.path.join(_common_pref_dir(), ".cache", "exports")
    except Exception:
        import tempfile
        exports_dir = os.path.join(tempfile.gettempdir(), "MetaZone_working")
    try:
        if exports_dir and os.path.isdir(exports_dir):
            for entry in os.listdir(exports_dir):
                p = os.path.join(exports_dir, entry)
                if os.path.isfile(p):
                    os.remove(p)
    except Exception:
        pass


def clear_thumb_cache():
    """Wipes every cached thumbnail file. Deliberately only ever touches
    files INSIDE the .cache/thumbs folder — prefs.json lives one level up
    in the common MetaZone folder itself and is never in scope here, by
    construction, not just by convention."""
    cache_dir = _thumb_cache_dir()
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0
    n = 0
    for entry in os.listdir(cache_dir):
        p = os.path.join(cache_dir, entry)
        try:
            if os.path.isfile(p):
                os.remove(p)
                n += 1
        except Exception:
            pass
    return n

def prefetch_thumb_to_cache(path, size=None, min_edge=None, max_edge=170):
    """Resizes and writes ONE thumbnail straight to the disk cache —
    deliberately never constructs a CTkImage or touches Tk at all, so
    this is safe to call from as many background threads as needed for
    a whole-batch prefetch (see main_window._prefetch_all_thumbnails),
    not just the bounded single-widget worker pool that make_thumb/
    make_thumb_min_edge use when a card is actually on screen."""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in VECTOR_EXTS or ext in VIDEO_EXTS:
            return
        cache_dir = _thumb_cache_dir()
        if not cache_dir:
            return
        if min_edge:
            key = _thumb_cache_key(path, f"edge{min_edge}x{max_edge}")
        else:
            size = size or (120, 85)
            key = _thumb_cache_key(path, f"box{size[0]}x{size[1]}")
        cache_path = os.path.join(cache_dir, key + ".jpg")
        if os.path.exists(cache_path):
            return  # already cached — nothing to do
        img = Image.open(path).convert("RGB")
        if min_edge:
            w, h = img.size
            if w <= 0 or h <= 0:
                return
            short = min(w, h)
            scale = min_edge / float(short)
            new_w, new_h = int(round(w * scale)), int(round(h * scale))
            if max(new_w, new_h) > max_edge:
                scale2 = max_edge / float(max(new_w, new_h))
                new_w, new_h = int(round(new_w * scale2)), int(round(new_h * scale2))
            img = img.resize((max(new_w, 1), max(new_h, 1)), Image.LANCZOS)
        else:
            img.thumbnail(size, Image.LANCZOS)
        img.save(cache_path, "JPEG", quality=85)
        _thumb_cache_cleanup(cache_dir)
    except Exception:
        pass

def find_file(folder,name,match_ext):
    exact=os.path.join(folder,name)
    if os.path.exists(exact): return exact
    if match_ext:
        base=os.path.splitext(name)[0]
        try:
            for f in os.listdir(folder):
                if os.path.splitext(f)[0].lower()==base.lower():
                    return os.path.join(folder,f)
        except: pass
    return None

def find_recursive(folder,name,match_ext):
    r=find_file(folder,name,match_ext)
    if r: return r
    try:
        for root,dirs,files in os.walk(folder):
            if root==folder: continue
            r=find_file(root,name,match_ext)
            if r: return r
    except: pass
    return None

def build_file_index(folder,recursive):
    """One-time directory scan producing {lowercased filename: full path}
    and {lowercased filename-without-extension: full path} lookup dicts.

    This exists because find_file/find_recursive, called once PER CSV ROW
    (as the embed flow used to do, for both its live match-count preview
    and the real embed pass), meant an N-row CSV with subfolder search on
    did up to N entirely independent `os.walk` scans of the same real
    folder tree — for a real nested folder structure and a 70+ row batch,
    with up to 6 of those walks running concurrently against each other
    (the embed pass's own worker pool), this is a genuine, confirmed
    root cause of an app freeze with no loading indicator, not a guess:
    walking the same large tree 70 times over instead of once is slow
    enough on its own, and doing several of those walks concurrently
    against the same disk/OS directory cache makes it worse, not faster.
    Building the index ONCE and doing O(1) dict lookups against it for
    every row (see index_lookup) turns that into a single scan total,
    regardless of how many rows or how many concurrent workers use it."""
    exact={}; stem={}
    def _add(root,files):
        for f in files:
            fp=os.path.join(root,f)
            exact.setdefault(f.lower(),fp)
            stem.setdefault(os.path.splitext(f)[0].lower(),fp)
    try:
        if recursive:
            for root,dirs,files in os.walk(folder):
                _add(root,files)
        else:
            _add(folder,os.listdir(folder))
    except Exception:
        pass
    return {"exact":exact,"stem":stem}

def index_lookup(index,name,match_ext):
    """O(1) equivalent of find_file/find_recursive against a pre-built
    build_file_index() result — same matching semantics (exact filename
    first, then extension-agnostic basename if match_ext is on)."""
    name=(name or "").strip()
    if not name or index is None: return None
    hit=index["exact"].get(name.lower())
    if hit: return hit
    if match_ext:
        return index["stem"].get(os.path.splitext(name)[0].lower())
    return None

def check_online():
    try:
        socket.setdefaulttimeout(3)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except Exception:
        return False

def _thumb_cache_dir():
    """Shared thumbnail cache — lives next to prefs.json in the same
    common MetaZone folder (see core/config.py's _common_pref_dir) so it
    persists across app versions the same way settings do."""
    try:
        from core.config import _common_pref_dir
        base = os.path.join(_common_pref_dir(), ".cache", "thumbs")
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".metazone_cache", "thumbs")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        return None
    return base

def _thumb_cache_key(path, tag):
    """mtime-keyed — a source file being replaced/edited automatically
    invalidates its cached thumbnail without needing to hash file
    contents (which would mean reading the whole file just to decide
    whether to skip reading the whole file)."""
    try:
        mtime = int(os.path.getmtime(path))
        size = os.path.getsize(path)
    except Exception:
        mtime, size = 0, 0
    raw = f"{os.path.abspath(path)}|{tag}|{mtime}|{size}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()

def _thumb_cache_cleanup(cache_dir, max_files=4000):
    """Automatic light cleanup — if the cache has grown past max_files,
    delete the oldest ones (by mtime) down to 90% of the cap. Cheap
    check (just a listdir) so it's fine to call opportunistically."""
    try:
        entries = os.listdir(cache_dir)
        if len(entries) <= max_files:
            return
        full = [os.path.join(cache_dir, e) for e in entries]
        full.sort(key=lambda p: os.path.getmtime(p))
        n_remove = len(full) - int(max_files * 0.9)
        for p in full[:n_remove]:
            try: os.remove(p)
            except Exception: pass
    except Exception:
        pass

def make_thumb(path, size=(120,85)):
    """Build a CTkImage off the main thread. Returns None on failure.
    Disk-cached: a resized copy is saved once and reused on later
    imports of the same file, instead of re-opening/re-resizing the
    full original image every single time."""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in VECTOR_EXTS or ext in VIDEO_EXTS:
            return None
        cache_dir = _thumb_cache_dir()
        cache_path = None
        if cache_dir:
            key = _thumb_cache_key(path, f"box{size[0]}x{size[1]}")
            cache_path = os.path.join(cache_dir, key + ".jpg")
            if os.path.exists(cache_path):
                try:
                    cimg = Image.open(cache_path); cimg.load()
                    return ctk.CTkImage(cimg, size=cimg.size)
                except Exception:
                    pass  # cache file corrupt/unreadable — fall through and regenerate
        img = Image.open(path)
        img = img.convert("RGB")
        img.thumbnail(size, Image.LANCZOS)
        if cache_path:
            try:
                img.save(cache_path, "JPEG", quality=85)
                _thumb_cache_cleanup(cache_dir)
            except Exception:
                pass
        return ctk.CTkImage(img, size=img.size)
    except Exception:
        return None


def make_thumb_min_edge(path, min_edge=100, max_edge=170):
    """Compact View's thumbnail: unlike make_thumb's bounding-box fit
    (both sides <= size), this scales so the SHORTER side is exactly
    min_edge and the longer side follows the image's own aspect ratio —
    capped at max_edge so an extreme panorama/vertical image can't blow
    out the card's layout. Disk-cached the same way as make_thumb."""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in VECTOR_EXTS or ext in VIDEO_EXTS:
            return None
        cache_dir = _thumb_cache_dir()
        cache_path = None
        if cache_dir:
            key = _thumb_cache_key(path, f"edge{min_edge}x{max_edge}")
            cache_path = os.path.join(cache_dir, key + ".jpg")
            if os.path.exists(cache_path):
                try:
                    cimg = Image.open(cache_path); cimg.load()
                    return ctk.CTkImage(cimg, size=cimg.size)
                except Exception:
                    pass
        img = Image.open(path)
        img = img.convert("RGB")
        w, h = img.size
        if w <= 0 or h <= 0:
            return None
        short = min(w, h)
        scale = min_edge / float(short)
        new_w, new_h = int(round(w * scale)), int(round(h * scale))
        if max(new_w, new_h) > max_edge:
            scale2 = max_edge / float(max(new_w, new_h))
            new_w, new_h = int(round(new_w * scale2)), int(round(new_h * scale2))
        new_w, new_h = max(new_w, 1), max(new_h, 1)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        if cache_path:
            try:
                img.save(cache_path, "JPEG", quality=85)
                _thumb_cache_cleanup(cache_dir)
            except Exception:
                pass
        return ctk.CTkImage(img, size=(new_w, new_h))
    except Exception:
        return None


def img_to_b64(path):
    with open(path,'rb') as f: data=f.read()
    ext=os.path.splitext(path)[1].lower()
    mime={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png',
          '.gif':'image/gif','.webp':'image/webp',
          '.tiff':'image/tiff','.tif':'image/tiff'}.get(ext,'image/jpeg')
    return base64.b64encode(data).decode(),mime

def _detect_and_fix_extension(fp):
    """If a file's real image format doesn't match its extension, ExifTool's
    format-specific writer refuses to touch it at all — rightly so, since
    writing (say) PNG-specific chunks into what's actually JPEG binary would
    corrupt the file, not just produce a warning. This is a real, fairly
    common occurrence with some AI image-generation tools that export
    JPEG-encoded data with a .png extension (or vice versa).

    There is no ExifTool flag that overrides this — verified directly
    against a real exiftool binary: `-m`, `-F`, `-api IgnoreMinorErrors=1`,
    and an explicit `-fileType=` override all still refuse, because they
    can't safely do otherwise; the file's actual bytes need the OTHER
    writer, unconditionally. The only correct fix is to give the file the
    extension that actually matches its content before writing.

    Returns (path_to_use, note) — note is None if nothing needed to change,
    otherwise a short human-readable description of what was renamed and
    why (meant for the caller's log/status output — never silent, since
    this does change the file's name on disk)."""
    ext=os.path.splitext(fp)[1].lower().lstrip(".")
    fmt_to_ext={"JPEG":"jpg","PNG":"png","WEBP":"webp","TIFF":"tif","BMP":"bmp","GIF":"gif"}
    try:
        with Image.open(fp) as im:
            real_fmt=im.format
    except Exception:
        return fp,None  # can't even open it -- let exiftool report its own error
    real_ext=fmt_to_ext.get(real_fmt)
    if not real_ext or real_ext==ext or (ext=="jpeg" and real_ext=="jpg"):
        return fp,None
    new_fp=os.path.splitext(fp)[0]+"."+real_ext
    if os.path.exists(new_fp):
        return fp,None  # don't clobber an existing file with that name
    try:
        os.rename(fp,new_fp)
        return new_fp,(f"{os.path.basename(fp)} was actually {real_fmt} data saved with a "
                        f".{ext} extension — renamed to {os.path.basename(new_fp)} to embed it")
    except Exception:
        return fp,None

def _is_eps_file(fp):
    """.ai is deliberately excluded: modern Illustrator .ai files are
    PDF-bodied (a completely different ExifTool writer path), so only
    a real .eps extension gets the EPS-specific write+verify path
    below. .ai keeps using the generic path unchanged."""
    return os.path.splitext(fp)[1].lower() == '.eps'


def _eps_header_intact(fp):
    """Cheap, format-appropriate sanity check that ExifTool's write did
    not corrupt the EPS/PostScript structure -- does NOT parse the
    vector content, dimensions, paths or colors (would require a real
    PostScript interpreter, out of scope here), only confirms the file
    still starts with a structure ExifTool/Illustrator/a RIP would
    recognize as EPS. Handles both plain ASCII EPS ("%!PS-Adobe...EPSF")
    and the binary "DOS EPS" wrapper (0xC5D0D3C6 magic, used when a
    preview image is embedded) -- ExifTool rewrites the binary header's
    length/offset fields when it edits a DOS EPS file, so this also
    confirms those fields still parse as sane (non-negative, offsets
    within the actual file size) rather than merely checking the magic
    bytes are present."""
    try:
        with open(fp, 'rb') as f:
            head = f.read(32)
    except Exception:
        return False, "Could not reopen file after write"
    if head[:4] == b'\xc5\xd0\xd3\xc6':
        import struct
        try:
            _, ps_start, ps_len, _, _, _, _ = struct.unpack('<IIIIIII', head[:28])
        except Exception:
            return False, "DOS EPS binary header is malformed after write"
        size = os.path.getsize(fp)
        if ps_len == 0 or ps_start + ps_len > size:
            return False, "DOS EPS binary header offsets no longer match the file after write"
        return True, "DOS EPS (binary preview) header intact"
    if head[:2] == b'%!':
        return True, "PostScript header intact"
    return False, "File no longer starts with a recognizable EPS/PostScript header"


def _ps_keyword_survival(ps_raw, keywords):
    """How many of `keywords` (in CSV order) actually survived, intact
    and in order, inside a single-line legacy PostScript-native DSC
    comment value (`ps_raw`, e.g. PostScript:Keywords/Subject read back
    from the file).

    v0.9.7.4 finding: real-world testing against an actual Illustrator-
    exported EPS10 file (not the minimal synthetic fixture the existing
    unit tests use) showed that ExifTool -- or possibly the specific
    combination of ExifTool and this file's own pre-existing DSC/XMP/
    Photoshop-IRB header content -- silently truncates an overlong value
    written to the single-line %%Keywords:/%%Subject: DSC comment. A
    49-keyword, 441-character comma-joined string came back with only
    the first 19 keywords (190 characters) after a real write+read-back
    round trip, even though IPTC:Keywords (a real list tag, not a
    single-line comment) held all 49 correctly in the same file. This is
    a genuine, format-level limitation of that one legacy field, not a
    CSV-loading, keyword-processing, or command-construction bug --
    those were all confirmed correct by the very fact that the complete
    49-keyword list *did* reach ExifTool and *did* land completely in
    IPTC:Keywords.

    Usefully, the truncation observed was clean: it stopped exactly at
    a keyword boundary (no partial word), so this walks the requested
    keyword list in order and counts how many are present as an exact,
    unbroken prefix run in the read-back string -- the count where a
    real reader would find good, complete keywords before the legacy
    field runs out of room. It does not just count "any keyword found
    anywhere" (a keyword occurring by coincidence out of order past a
    corrupted tail should not count as "surviving").

    Returns (survived_count: int, clean_cutoff: bool) -- clean_cutoff
    is False if the read-back has a trailing fragment that isn't a
    complete match for the next expected keyword (i.e. a partial/
    mid-word cut), which is worth surfacing even though it hasn't been
    observed in practice."""
    tokens = [t.strip() for t in (ps_raw or '').split(',') if t.strip()]
    wanted = [k.strip() for k in keywords if k.strip()]
    n = 0
    for i, tok in enumerate(tokens):
        if i < len(wanted) and tok.lower() == wanted[i].lower():
            n += 1
        else:
            clean_cutoff = (n == len(tokens))  # nothing unaccounted for before the mismatch
            return n, clean_cutoff
    return n, True


def _verify_eps_metadata(et, fp, title, keywords):
    """Reads the just-written tags straight back out of the file (a
    fresh ExifTool process, not a cached value) and confirms:
      - the file is still identified as EPS/PostScript,
      - the requested Title is fully present in the authoritative,
        uncapped field (if one was requested),
      - every requested keyword is fully present in the authoritative,
        uncapped field (if any were requested).

    v0.9.7.3 fix (kept): this used to read the *unscoped*
    "-Keywords"/"-Subject" tag names, which ExifTool's own group-
    priority order resolved to IPTC:Keywords even when the separate
    PostScript:Keywords DSC comment had been silently collapsed to just
    the last keyword -- so the old check kept reporting "verified" on a
    file that had actually lost data. Reading both groups explicitly
    with `-G1` catches a regression in either one.

    v0.9.7.4 fix: two related problems found via a real end-to-end test
    against an actual Illustrator-exported EPS10 file (see the project's
    diagnostic report):

    1. Title authority was PostScript:Title, not IPTC:Headline. That was
       deliberately chosen in v0.9.7.3 to dodge IPTC:ObjectName's real
       64-byte IIM cap -- correct as far as it went, but IPTC:Headline
       has no such cap either (IIM allows up to 256 bytes, comfortably
       more than any realistic title here) *and* is the field most
       stock-marketplace/DAM ingestion actually reads as "the title".
       Real testing found PostScript:Title round-trips fine for a
       191-character title but is not proven safe at the 198-character
       length this project's own longest CSV title reaches -- i.e. it
       may be subject to the same kind of single-line DSC length limit
       documented on _ps_keyword_survival above, just with a higher,
       less obviously-hit threshold. IPTC:Headline is now the pass/fail
       authority for title; PostScript:Title is still written
       (unchanged) and still checked, but only as a best-effort legacy
       field -- a mismatch there is reported, not failed.

    2. Keywords: the old code hard-failed the entire embed if a single
       keyword was missing from the single-line PostScript:Keywords/
       Subject DSC comment -- but that field provably cannot hold an
       arbitrarily long keyword list; it has a real, low, undocumented
       capacity ceiling that is a property of the file format/tooling,
       not of this app's code. Hard-failing on that made every EPS batch
       with more than roughly 19 keywords report as an *error* even
       though the complete, correct, standards-based data was sitting
       right there in IPTC:Keywords the whole time. IPTC:Keywords is now
       the pass/fail authority (as it already provably holds the
       complete list); the PostScript-native field is written and
       checked as before, but a shortfall there is reported as
       information, not failure.

    Returns (verified: bool, detail: str, diag: dict). diag always has
    the keys: file_type, title_iptc_ok, title_ps_ok, kw_total,
    kw_iptc_missing, kw_ps_survived, kw_ps_clean.
    """
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    cmd = [et, '-j', '-charset', 'UTF8', '-G1', '-FileType',
           '-IPTC:ObjectName', '-IPTC:Headline', '-PostScript:Title',
           '-IPTC:Keywords', '-PostScript:Keywords', '-PostScript:Subject',
           fp]
    diag = {"file_type": None, "title_iptc_ok": None, "title_ps_ok": None,
            "kw_total": len(keywords) if keywords else 0,
            "kw_iptc_missing": [], "kw_ps_survived": 0, "kw_ps_clean": True}
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=flags)
        data = json.loads(res.stdout or '[]')
        if not data:
            return False, "ExifTool produced no readable output on verification pass", diag
        entry = data[0]
    except Exception as ex:
        return False, f"Verification read failed: {ex}", diag

    file_type = (entry.get('File:FileType') or entry.get('FileType') or '').upper()
    diag["file_type"] = file_type
    if file_type not in ('EPS', 'PS'):
        return False, f"File no longer identifies as EPS after write (FileType={file_type or 'unknown'})", diag

    warnings = []

    if title:
        wanted_title = title.strip()
        # Authoritative: IPTC:Headline -- no meaningful cap for any
        # realistic title (IIM allows up to 256 bytes), and this is the
        # field most external tooling actually reads as "the title".
        read_headline = (entry.get('IPTC:Headline') or '').strip()
        diag["title_iptc_ok"] = (read_headline == wanted_title)
        if not diag["title_iptc_ok"]:
            return False, f"Title not found on read-back (wrote {wanted_title!r}, read {read_headline!r})", diag

        # Best-effort only: the legacy single-line %%Title: DSC comment.
        # Written the same as before; checked, but a mismatch here is a
        # known format-capacity limitation, not a failure -- see
        # _ps_keyword_survival's docstring for the analogous keywords case.
        read_ps_title = (entry.get('PostScript:Title') or '').strip()
        diag["title_ps_ok"] = (read_ps_title == wanted_title)
        if not diag["title_ps_ok"]:
            warnings.append(f"legacy PostScript:Title field holds {read_ps_title!r} "
                             f"(truncated/mismatched -- single-line DSC format limit; "
                             f"full title is present in IPTC:Headline)")

    if keywords:
        iptc_raw = entry.get('IPTC:Keywords') or []
        if isinstance(iptc_raw, str):
            iptc_raw = [iptc_raw]
        iptc_present = {k.strip().lower() for k in iptc_raw}
        missing_iptc = [k for k in keywords if k.strip().lower() not in iptc_present]
        diag["kw_iptc_missing"] = missing_iptc
        if missing_iptc:
            return False, (f"{len(missing_iptc)} of {len(keywords)} keyword(s) missing from "
                            f"IPTC:Keywords on read-back: {', '.join(missing_iptc[:5])}"), diag

        # Best-effort only: the legacy single-line %%Keywords:/%%Subject:
        # DSC comment has a real, low capacity ceiling (see
        # _ps_keyword_survival) -- report how many whole keywords made it
        # in, but don't fail the embed over it. The complete list is
        # already confirmed present in IPTC:Keywords above.
        ps_raw = (entry.get('PostScript:Keywords') or entry.get('PostScript:Subject') or '')
        survived, clean = _ps_keyword_survival(ps_raw, keywords)
        diag["kw_ps_survived"] = survived
        diag["kw_ps_clean"] = clean
        if survived < len(keywords):
            warnings.append(f"legacy PostScript-native Keywords field holds {survived} of "
                             f"{len(keywords)} keyword(s){'' if clean else ' (last entry looks truncated mid-word)'} "
                             f"-- single-line DSC format limit; the complete list is present in IPTC:Keywords")

    detail = "Title/keywords verified complete in IPTC (the authoritative, uncapped fields)"
    if warnings:
        detail += "; " + "; ".join(warnings)
    return True, detail, diag


def embed_metadata_one(et, fp, title="", kw_raw="", desc="", rm_prog=False, rm_copy=False):
    """Write title/keywords/description into a single file via ExifTool.
    Pure function, no UI/state -- shared by ui/embed_window.py's CSV-driven
    embed flow and smart_workflow's Stage 6, so the exiftool command
    construction only lives in one place.
    Returns (ok: bool, message: str, final_path: str) -- final_path is
    normally just fp unchanged, but may differ if a real/extension format
    mismatch was found and fixed first (see _detect_and_fix_extension);
    callers that do anything else with the file afterward (renaming to
    title, logging a filename) should use final_path, not the fp they
    passed in, since fp may no longer exist under that name.

    EPS10 note (Problem A / fixed properly in v0.9.7.3): a real .eps
    file used to get the exact same command as every other format,
    repeating `-Keywords=kw`/`-Subject=kw` once per keyword. Those tag
    names ARE ExifTool's real, documented write targets for
    EPS/PostScript -- the choice of tags was never wrong -- but
    "Keywords"/"Subject" resolve to TWO different fields on an EPS
    file: IPTC:Keywords (a real list tag, where each repeat correctly
    appends) and PostScript:Keywords (a single-string DSC "%%Keywords:"
    comment, where each repeat *overwrites* the last write). Confirmed
    with a real exiftool round-trip: writing `-Keywords=a -Keywords=b
    -Keywords=c` left IPTC:Keywords correct ("a, b, c") but silently
    collapsed PostScript:Keywords down to just "c" -- every keyword but
    the last was quietly lost from the PostScript-native field despite
    ExifTool returning exit code 0. Some stock-platform ingestion paths
    and older EPS/PostScript tooling read the PostScript-native field
    rather than the embedded IPTC block, so this was a real, silent
    data-loss bug even though the write "succeeded".

    Fix: for .eps files only, Keywords/Subject are written as two
    separate, explicitly group-scoped operations instead of one
    generic repeated one -- `-IPTC:Keywords=` repeated per keyword
    (list tag, safe to repeat) plus a single `-PostScript:Keywords=`/
    `-PostScript:Subject=` write of the SAME keywords already
    comma-joined into one string (matching the single-string type that
    field actually is). JPEG/PNG/.ai and every other format are
    untouched -- they still go through the original generic command
    below, since the ambiguous-tag-name collision this fixes is
    specific to how ExifTool maps "Keywords"/"Subject" onto the
    PostScript group.

    For .eps specifically the write is followed by an actual read-back
    verification (_verify_eps_metadata, corrected in v0.9.7.3 and again
    in v0.9.7.4 -- see its docstring) and a structural sanity check
    (_eps_header_intact) before reporting success. A returncode of 0
    from ExifTool is necessary but not sufficient -- see both helper
    functions' docstrings.

    v0.9.7.4 note: a real end-to-end test against an actual Illustrator-
    exported EPS10 file found that the legacy single-line PostScript-
    native %%Keywords:/%%Subject:/%%Title: DSC comments have a real,
    low capacity ceiling that this app's CSV-driven keyword/title
    lengths can and do exceed (49 keywords / a ~200-character title are
    completely ordinary for this workflow). That is a property of the
    file format and/or ExifTool's EPS writer, not of the code below --
    the full, untruncated title and keyword list are confirmed to reach
    ExifTool intact and land completely in the IPTC fields (IPTC:Headline,
    IPTC:Keywords) every time. _verify_eps_metadata now treats IPTC as
    the pass/fail authority for completeness and reports (without
    failing) whatever the legacy PostScript-native fields could actually
    hold -- see its docstring and _ps_keyword_survival for the full
    reasoning."""
    fp,rename_note=_detect_and_fix_extension(fp)
    is_eps = _is_eps_file(fp)
    kw_list=[k.strip() for k in kw_raw.replace(';',',').split(',') if k.strip()] if kw_raw else []

    if is_eps:
        # De-duplicated (case-insensitive, first-occurrence order kept) only
        # for the single comma-joined PostScript string -- avoids writing
        # "cat, cat, dog" into the DSC comment if the CSV had a repeated
        # keyword. The per-keyword IPTC:Keywords list below is left as-is,
        # matching the existing (unchanged) dedup behavior of every other
        # format's Keywords list.
        seen=set(); kw_dedup=[]
        for kw in kw_list:
            key=kw.lower()
            if key not in seen:
                seen.add(key); kw_dedup.append(kw)

        cmd=[et,'-overwrite_original','-codedcharacterset=UTF8']
        if title:
            cmd+=[f'-IPTC:ObjectName={title}', f'-IPTC:Headline={title}', f'-PostScript:Title={title}']
        for kw in kw_list:
            cmd+=[f'-IPTC:Keywords={kw}']
        if kw_dedup:
            joined=', '.join(kw_dedup)
            cmd+=[f'-PostScript:Keywords={joined}', f'-PostScript:Subject={joined}']
        if desc:
            cmd+=[f'-IPTC:Caption-Abstract={desc}']
        # rm_prog/rm_copy are single-value removals (never repeated in a
        # loop), so they were never subject to the list-vs-single-string
        # collision this fix addresses -- left as the original unscoped
        # tags, unchanged, rather than guessing at new group-scoped names.
        if rm_prog: cmd+=['-Software=','-CreatorTool=','-HistorySoftwareAgent=']
        if rm_copy: cmd+=['-Rights=','-Copyright=','-CopyrightNotice=','-Creator=']
    else:
        cmd=[et,'-overwrite_original','-codedcharacterset=UTF8']
        if title: cmd+=[f'-Title={title}',f'-ObjectName={title}',f'-Headline={title}']
        for kw in kw_list:
            cmd+=[f'-Keywords={kw}',f'-Subject={kw}']
        if desc: cmd+=[f'-Description={desc}',f'-Caption-Abstract={desc}']
        if rm_prog: cmd+=['-Software=','-CreatorTool=','-HistorySoftwareAgent=']
        if rm_copy: cmd+=['-Rights=','-Copyright=','-CopyrightNotice=','-Creator=']
    cmd.append(fp)
    try:
        flags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
        res=subprocess.run(cmd,capture_output=True,text=True,timeout=30,creationflags=flags)
        if res.returncode!=0:
            return False, (res.stderr or res.stdout or "Unknown").strip(), fp

        eps_note = None
        if is_eps:
            intact, intact_msg = _eps_header_intact(fp)
            if not intact:
                return False, f"EPS write command succeeded but the file is corrupted afterward: {intact_msg}", fp
            verified, verify_msg, verify_diag = _verify_eps_metadata(et, fp, title, kw_list)
            if not verified:
                return False, f"EPS write command succeeded but metadata failed verification: {verify_msg}", fp
            # v0.9.7.4: verify_msg already distinguishes "fully verified" from
            # "verified in IPTC, but the legacy PostScript-native field is
            # capacity-limited" -- surface that distinction in the per-file
            # log line instead of a single unconditional "verified" claim,
            # so a real, format-level shortfall in the legacy field is never
            # silently reported the same way as a full, clean pass.
            ps_kw_total = verify_diag.get("kw_total") or 0
            ps_kw_survived = verify_diag.get("kw_ps_survived") or 0
            title_ps_ok = verify_diag.get("title_ps_ok")
            if (ps_kw_total and ps_kw_survived < ps_kw_total) or (title_ps_ok is False):
                bits = []
                if ps_kw_total:
                    bits.append(f"{ps_kw_survived}/{ps_kw_total} keywords")
                if title_ps_ok is False:
                    bits.append("title truncated")
                eps_note = (f"EPS metadata verified — IPTC complete; legacy PostScript-native "
                            f"comment holds {', '.join(bits)} (single-line DSC format limit)")
            else:
                eps_note = "EPS metadata verified on read-back"

        msg=os.path.basename(fp)
        if rename_note: msg=f"{msg}  ({rename_note})"
        if eps_note: msg=f"{msg}  ({eps_note})"
        return True, msg, fp
    except Exception as ex:
        return False, str(ex), fp

def read_existing_titles_batch(et, filepaths, batch_size=200):
    """v0.9.4.1: for Dry Run's "Already Embedded" detection. Reads the
    Title tag from a whole list of files in one (or a few, chunked)
    exiftool subprocess call(s) via -j (JSON output) rather than one
    subprocess per file -- exiftool has natively supported multiple
    file arguments per invocation since long before this app existed,
    so this is not new/fragile behavior, just a batch read instead of
    a per-file one. Chunked at batch_size to keep the command line
    length reasonable on very large folders (Windows has a real
    command-line length ceiling); each chunk is still exactly one
    subprocess call. Returns {filepath: title_string_or_empty}. Best-
    effort: if exiftool fails or isn't found, returns {} and the
    caller should treat every file as "unknown" (see dry_run in
    embedder.py), never as a false "already embedded".
    """
    result = {}
    if not filepaths:
        return result
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    for i in range(0, len(filepaths), batch_size):
        chunk = filepaths[i:i + batch_size]
        cmd = [et, '-j', '-Title', '-charset', 'UTF8'] + chunk
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=flags)
            if res.returncode not in (0, 1):  # exiftool uses 1 for "minor warnings, still produced output"
                continue
            data = json.loads(res.stdout or '[]')
            for entry in data:
                src = entry.get('SourceFile')
                if src:
                    result[src] = (entry.get('Title') or '').strip()
        except Exception:
            continue  # best-effort -- a failed batch just leaves those files unclassified, never misclassified
    return result

def format_filesize(path):
    try:
        n=os.path.getsize(path)
    except Exception:
        return "—"
    for unit in ("B","KB","MB","GB"):
        if n<1024:
            return f"{n:.0f} {unit}" if unit=="B" else f"{n:.1f} {unit}"
        n/=1024
    return f"{n:.1f} TB"

def relaunch_app():
    """Close this process and start a fresh one. Used after applying a
    new theme — colors are derived once at ui/theme.py's import time
    (see that module), so a genuinely fresh process is what makes a new
    choice actually take effect, not a live-reactive rebuild. Handles
    both running from source (python app.py) and the packaged frozen
    EXE — a frozen build has no Python interpreter to hand a script to,
    so it must re-launch sys.executable directly with no arguments.

    IMPORTANT (frozen/onefile only): the child MUST NOT inherit this
    process's _MEIPASS2 env var. PyInstaller's onefile bootloader sets
    _MEIPASS2 internally once it has extracted itself; if a relaunched
    child inherits that value (subprocess.Popen inherits the full
    environment by default), its bootloader assumes it's already
    extracted and tries to run directly off THIS process's temp folder
    instead of doing its own fresh extraction. That folder disappears
    once this process exits, so the child breaks the moment you relaunch
    a second time in the same session (the first relaunch can appear to
    work purely because the old temp folder hasn't been cleaned up yet).

    Two more failure modes seen specifically on a SECOND relaunch in one
    session (theme changed twice in a row): a 'Failed to remove
    temporary directory' warning from the bootloader, paired with a
    false 'ExifTool missing' status on the freshly-relaunched window.
    Both point at the same underlying race — Windows/antivirus hasn't
    finished releasing its lock on THIS process's _MEIxxxxxx extraction
    folder (which holds exiftool.exe) at the exact moment we vanish and
    the bootloader tries to clean it up, and/or the new child inherited
    a cwd that no longer exists once this process is gone. Neither is
    fully controllable from here, but both are mitigated by: never
    inheriting this process's (soon-to-be-gone) cwd, confirming the
    child actually started before we exit, and giving the OS a brief
    moment before we do."""
    try:
        env=os.environ.copy()
        env.pop("_MEIPASS2",None)
        # Never hand the child a cwd that lives inside THIS process's
        # onefile temp extraction — that folder is on its way out the
        # moment we exit, and a child that inherited it as its working
        # directory can fail in ways that look identical to a missing
        # bundled file (ExifTool included) even though it extracted fine.
        safe_cwd=os.path.expanduser("~") or None
        if getattr(sys,"frozen",False):
            proc=subprocess.Popen([sys.executable],env=env,cwd=safe_cwd)
        else:
            main_mod=sys.modules.get("__main__")
            main_file=getattr(main_mod,"__file__",None)
            proc=None
            if main_file:
                proc=subprocess.Popen([sys.executable,os.path.abspath(main_file)],
                    env=env,cwd=safe_cwd)
        # Confirm the child is actually alive (didn't crash on the spot)
        # before we disappear — and give Windows/AV a brief window to
        # settle file locks on this process's temp folder before the
        # bootloader tries to remove it.
        if proc is not None:
            time.sleep(0.35)
            proc.poll()  # refresh returncode; ignored either way — best-effort only
        time.sleep(0.15)
    finally:
        os._exit(0)

