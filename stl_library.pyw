"""
STL Library (Windows)
- Thumbnail grid of your STL files (searchable).
- Hover a thumbnail: the model spins.
- Click a thumbnail: the file opens in ChiTuBox Basic.
- Right-click a thumbnail: show the file in Explorer.

Thumbnails come from Windows' own thumbnail cache (via the Explorer shell API)
when Windows can provide one, otherwise from stl-thumb.

Settings are stored in %APPDATA%\\STLLibrary\\config.json. The thumbnail and
spin-preview cache can live on any drive, including an external one.
"""
import ctypes
import glob
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageDraw, ImageTk

SLICER_NAME = "ChiTuBox Basic"
APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "STLLibrary"
CONFIG_FILE = APP_DIR / "config.json"
DEFAULT_CACHE = APP_DIR / "cache"
THUMB_SIZE = 200
COLUMNS = 5
SPIN_FRAMES = 24
SPIN_CACHE_MODELS = 20
ALL = "All sources"
NO_WINDOW = 0x08000000  # stops a console window flashing for each stl-thumb call


# ================================================================ config ====
def find_slicer() -> str:
    """Best-effort search for the ChiTuBox Basic executable."""
    bad = ("uninst", "unins", "crash", "update", "redist", "dump", "helper")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    pats = []
    for base in (pf, pf86, os.path.join(local, "Programs") if local else ""):
        if base:
            pats += [os.path.join(base, "*CHITUBOX*", "*.exe"),
                     os.path.join(base, "*CHITUBOX*", "*", "*.exe")]
    hits = []
    for p in pats:
        for h in glob.glob(p):
            name = os.path.basename(h).lower()
            if "chitubox" in name and not any(b in name for b in bad):
                hits.append(h)
    return sorted(hits)[0] if hits else ""


def find_stl_thumb() -> str:
    return shutil.which("stl-thumb") or shutil.which("stl-thumb.exe") or ""


def load_config() -> dict:
    cfg = {
        "sources": [],
        "models_folder": "",
        "cache_folder": str(DEFAULT_CACHE),
        "slicer_path": find_slicer(),
        "stl_thumb_path": find_stl_thumb(),
        "use_windows_thumbs": True,
    }
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if not cfg.get("sources") and cfg.get("models_folder"):
        cfg["sources"] = [cfg["models_folder"]]  # migrate older single-folder setup
    if not cfg.get("slicer_path"):
        cfg["slicer_path"] = find_slicer()
    return cfg


def save_config(cfg: dict):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def usable_cache_dir(cfg: dict):
    """(path, warning). Falls back to the default cache if the chosen drive is
    missing, e.g. the external drive is unplugged."""
    try:
        p = Path(cfg["cache_folder"])
        p.mkdir(parents=True, exist_ok=True)
        return p, ""
    except Exception:
        DEFAULT_CACHE.mkdir(parents=True, exist_ok=True)
        return DEFAULT_CACHE, f"(cache drive not available, using {DEFAULT_CACHE})"


def file_key(stl: Path) -> str:
    st = stl.stat()
    return hashlib.md5(f"{stl}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()


_TOP_LEVEL_SKIP = {"windows", "program files", "program files (x86)", "programdata", "appdata"}


def scan_sources(sources):
    """Walk every source (folder, whole drive, or single file) and collect STL files.
    Returns (found, missing): found is a list of (Path, source_label); missing lists
    sources that are not reachable right now, e.g. an unplugged drive."""
    found, seen, missing = [], set(), []

    def add(path, label):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            found.append((Path(path), label))

    for src in sources:
        if not os.path.exists(src):
            missing.append(src)
            continue
        if os.path.isfile(src):
            if src.lower().endswith(".stl"):
                add(src, "(single files)")
            continue
        top = os.path.normcase(os.path.abspath(src))
        for root, dirs, names in os.walk(src, onerror=lambda e: None):
            at_top = os.path.normcase(os.path.abspath(root)) == top
            dirs[:] = [d for d in dirs
                       if not d.startswith("$") and d.lower() != "system volume information"
                       and not (at_top and d.lower() in _TOP_LEVEL_SKIP)]
            for n in names:
                if n.lower().endswith(".stl") and not n.startswith("._"):
                    add(os.path.join(root, n), src)
    return found, missing


# ===================================================== Windows thumbnails ===
class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_IID_IShellItemImageFactory = "bcc18b79-ba16-442f-80c4-8a59c30c463b"
_SIIGBF_THUMBNAILONLY = 0x8  # fail instead of returning a generic icon


def shell_thumbnail(path: Path, size: int):
    """Ask Windows Explorer for this file's thumbnail (served from the Windows
    thumbnail cache when present, generated by an installed handler otherwise).
    Returns a square RGBA PIL image, or None if Windows has no thumbnail."""
    try:
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
    except AttributeError:  # not Windows
        return None

    shell32.SHCreateItemFromParsingName.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    hr = ole32.CoInitialize(None)
    inited = hr in (0, 1)
    factory = ctypes.c_void_p()
    try:
        iid = _GUID.from_buffer_copy(uuid.UUID(_IID_IShellItemImageFactory).bytes_le)
        if shell32.SHCreateItemFromParsingName(str(path), None, ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return None
        vtbl = ctypes.cast(ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p))[0],
                           ctypes.POINTER(ctypes.c_void_p))
        get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, _SIZE, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_void_p))(vtbl[3])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        hbm = ctypes.c_void_p()
        try:
            if get_image(factory, _SIZE(size, size), _SIIGBF_THUMBNAILONLY, ctypes.byref(hbm)) != 0 or not hbm.value:
                return None
        finally:
            release(factory)
            factory = ctypes.c_void_p()

        bm = _BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = bm.bmWidth, bm.bmHeight
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(bmi)
        bmi.biWidth, bmi.biHeight = w, -h  # negative = top-down
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = user32.GetDC(None)
        gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), 0)
        user32.ReleaseDC(None, hdc)
        gdi32.DeleteObject(hbm)

        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        if img.getchannel("A").getextrema() == (0, 0):  # bitmap carried no alpha
            img = img.convert("RGB").convert("RGBA")
        return img
    except Exception:
        return None
    finally:
        if inited:
            ole32.CoUninitialize()


def _pad_square(img: Image.Image, size: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((size, size))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
    return canvas


def make_thumb(stl: Path, cache_dir: Path, cfg: dict):
    """Static thumbnail PNG path (from cache, Windows, or stl-thumb) or None."""
    try:
        out = cache_dir / f"{file_key(stl)}.png"
        if out.exists():
            return out
        if cfg.get("use_windows_thumbs", True):
            img = shell_thumbnail(stl, THUMB_SIZE)
            if img is not None:
                _pad_square(img, THUMB_SIZE).save(out)
                return out
        tool = cfg.get("stl_thumb_path")
        if tool:
            subprocess.run([tool, str(stl), str(out), "-s", str(THUMB_SIZE)],
                           check=True, capture_output=True, timeout=60, creationflags=NO_WINDOW)
            return out if out.exists() else None
    except Exception:
        pass
    return None


# ========================================================== spin renderer ===
def load_stl(path: Path) -> np.ndarray:
    """Return triangles as float32 array of shape (N, 3, 3). Binary or ASCII."""
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = int.from_bytes(data[80:84], "little")
        if 84 + n * 50 == len(data):
            dt = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("a", "<u2")])
            return np.frombuffer(data, dtype=dt, count=n, offset=84)["v"].astype(np.float32)
    nums = re.findall(r"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)",
                      data.decode("utf-8", "ignore"))
    if not nums:
        raise ValueError("no triangles found")
    return np.array(nums, dtype=np.float64).astype(np.float32).reshape(-1, 3, 3)


def simplify(tris: np.ndarray, max_tris: int = 20000) -> np.ndarray:
    """Cheap vertex-clustering decimation so big meshes still spin quickly.
    Vertices in the same grid cell merge into their average position."""
    if len(tris) <= max_tris:
        return tris
    flat = tris.reshape(-1, 3).astype(np.float64)
    mn = flat.min(0)
    span = float((flat.max(0) - mn).max()) or 1.0
    res = 120.0
    while True:
        cells = np.floor((flat - mn) / span * res).astype(np.int64)
        key = (cells[:, 0] * 100003 + cells[:, 1]) * 100003 + cells[:, 2]
        _, inv = np.unique(key, return_inverse=True)
        inv = inv.reshape(-1)
        counts = np.bincount(inv)
        centers = np.stack([np.bincount(inv, weights=flat[:, a]) for a in range(3)], 1) / counts[:, None]
        idx = inv.reshape(-1, 3)
        keep = (idx[:, 0] != idx[:, 1]) & (idx[:, 1] != idx[:, 2]) & (idx[:, 0] != idx[:, 2])
        idx = np.unique(idx[keep], axis=0)
        if len(idx) <= max_tris or res < 12:
            return centers[idx].astype(np.float32)
        res *= 0.8


def render_spin(stl: Path, n: int = SPIN_FRAMES, size: int = THUMB_SIZE, ss: int = 2):
    """Software-render n frames of the model turning around its Z axis."""
    tris = simplify(load_stl(stl)).astype(np.float64)
    pts = tris.reshape(-1, 3)
    tris = tris - (pts.min(0) + pts.max(0)) / 2
    radius = float(np.sqrt((tris ** 2).sum(-1)).max()) or 1.0
    tris /= radius

    tilt = np.radians(25)
    ct, st = np.cos(tilt), np.sin(tilt)
    rx = np.array([[1, 0, 0], [0, ct, -st], [0, st, ct]])
    light = np.array([-0.4, -0.7, 0.6])
    light /= np.linalg.norm(light)
    base = np.array([170, 185, 205], dtype=np.float64)
    big = size * ss
    scale = big * 0.46

    frames = []
    for k in range(n):
        th = 2 * np.pi * k / n
        c, s = np.cos(th), np.sin(th)
        rz = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        v = tris @ (rx @ rz).T                       # rotate every vertex
        nrm = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
        ln = np.linalg.norm(nrm, axis=1)
        ln[ln == 0] = 1
        nrm /= ln[:, None]
        shade = 0.30 + 0.70 * np.abs(nrm @ light)    # two-sided lighting
        order = np.argsort(-v[:, :, 1].mean(1))      # far to near (painter's algorithm)
        xy = np.stack([big / 2 + v[:, :, 0] * scale, big / 2 - v[:, :, 2] * scale], -1)
        polys = xy[order].reshape(-1, 6).tolist()
        cols = np.empty((len(tris), 4), dtype=np.uint8)
        cols[:, :3] = (base[None, :] * shade[:, None]).astype(np.uint8)
        cols[:, 3] = 255
        cols = [tuple(c4) for c4 in cols[order].tolist()]

        img = Image.new("RGBA", (big, big), (170, 185, 205, 0))
        d = ImageDraw.Draw(img)
        for poly, col in zip(polys, cols):
            d.polygon(poly, fill=col)
        frames.append(img.resize((size, size), Image.LANCZOS))
    return frames


def load_or_render_spin(stl: Path, cache_dir: Path):
    strip = cache_dir / f"spin_{file_key(stl)}.png"
    if strip.exists():
        try:
            sheet = Image.open(strip).convert("RGBA")
            return [sheet.crop((i * THUMB_SIZE, 0, (i + 1) * THUMB_SIZE, THUMB_SIZE))
                    for i in range(sheet.width // THUMB_SIZE)]
        except Exception:
            pass
    frames = render_spin(stl)
    try:
        sheet = Image.new("RGBA", (THUMB_SIZE * len(frames), THUMB_SIZE))
        for i, f in enumerate(frames):
            sheet.paste(f, (i * THUMB_SIZE, 0))
        sheet.save(strip)
    except Exception:
        pass
    return frames


# ============================================================== settings ====
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg, on_save):
        super().__init__(parent)
        self.title("Settings")
        self.cfg, self.on_save = cfg, on_save
        self.transient(parent)
        self.grab_set()
        self.vars = {}
        rows = [
            ("cache_folder", "Thumbnail cache folder", "dir"),
            ("slicer_path", f"{SLICER_NAME} executable", "file"),
            ("stl_thumb_path", "stl-thumb executable (fallback)", "file"),
        ]
        for i, (key, label, kind) in enumerate(rows):
            tk.Label(self, text=label, anchor="w").grid(row=i, column=0, sticky="w", padx=8, pady=6)
            v = tk.StringVar(value=cfg.get(key, ""))
            self.vars[key] = v
            tk.Entry(self, textvariable=v, width=62).grid(row=i, column=1, padx=4)
            tk.Button(self, text="Browse...", command=lambda v=v, k=kind: self.browse(v, k)).grid(row=i, column=2, padx=8)
        n = len(rows)
        self.win_thumbs = tk.BooleanVar(value=cfg.get("use_windows_thumbs", True))
        tk.Checkbutton(self, text="Use Windows Explorer thumbnails when available (falls back to stl-thumb)",
                       variable=self.win_thumbs).grid(row=n, column=0, columnspan=3, sticky="w", padx=8)
        tk.Label(self, fg="gray40",
                 text="Tip: the cache can be on an external drive. Thumbnails and spin previews are only rebuilt if missing."
                 ).grid(row=n + 1, column=0, columnspan=3, padx=8, pady=(2, 8), sticky="w")
        tk.Button(self, text="Save", width=12, command=self.save).grid(row=n + 2, column=1, pady=10, sticky="e")
        tk.Button(self, text="Cancel", width=10, command=self.destroy).grid(row=n + 2, column=2, padx=8)

    def browse(self, var, kind):
        if kind == "dir":
            p = filedialog.askdirectory(initialdir=var.get() or None)
        else:
            p = filedialog.askopenfilename(initialdir=os.path.dirname(var.get()) or None,
                                           filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if p:
            var.set(os.path.normpath(p))

    def save(self):
        for k, v in self.vars.items():
            self.cfg[k] = v.get().strip()
        self.cfg["use_windows_thumbs"] = bool(self.win_thumbs.get())
        save_config(self.cfg)
        self.on_save()
        self.destroy()


class LibraryDialog(tk.Toplevel):
    """Manage the folders, drives and single files that make up the library."""

    def __init__(self, parent, cfg, on_done):
        super().__init__(parent)
        self.title("Library folders and drives")
        self.cfg, self.on_done = cfg, on_done
        self.transient(parent)
        self.grab_set()
        tk.Label(self, justify="left", fg="gray30",
                 text="Folders are scanned including every subfolder. To add a whole drive, pick its root "
                      "(for example D:\\).\nDrives that are unplugged are skipped and shown as not connected."
                 ).pack(anchor="w", padx=10, pady=(10, 4))
        frame = tk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10)
        self.listbox = tk.Listbox(frame, width=85, height=12, selectmode="extended")
        sb = tk.Scrollbar(frame, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        btns = tk.Frame(self)
        btns.pack(fill="x", padx=10, pady=10)
        tk.Button(btns, text="Add folder or drive...", command=self.add_folder).pack(side="left")
        tk.Button(btns, text="Add files...", command=self.add_files).pack(side="left", padx=6)
        tk.Button(btns, text="Remove selected", command=self.remove).pack(side="left")
        tk.Button(btns, text="Done", width=10, command=self.done).pack(side="right")
        self.refresh()

    def refresh(self):
        self.listbox.delete(0, "end")
        for src in self.cfg["sources"]:
            self.listbox.insert("end", src + ("" if os.path.exists(src) else "     [not connected]"))

    def _add(self, paths):
        for p in paths:
            p = os.path.normpath(p)
            if p not in self.cfg["sources"]:
                self.cfg["sources"].append(p)
        save_config(self.cfg)
        self.refresh()

    def add_folder(self):
        p = filedialog.askdirectory(parent=self, title="Choose a folder or drive to add")
        if p:
            self._add([p])

    def add_files(self):
        ps = filedialog.askopenfilenames(parent=self, title="Choose STL files to add",
                                         filetypes=[("STL files", "*.stl"), ("All files", "*.*")])
        if ps:
            self._add(ps)

    def remove(self):
        for i in reversed(self.listbox.curselection()):
            del self.cfg["sources"][i]
        save_config(self.cfg)
        self.refresh()

    def done(self):
        save_config(self.cfg)
        cb = self.on_done
        self.destroy()
        cb()


# ================================================================== app =====
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("STL Library")
        self.geometry("1150x780")
        self.cfg = load_config()
        self.files, self.buttons, self.static = [], {}, {}
        self.file_label, self.missing, self.scan_id = {}, [], 0
        self.spin_cache, self.spin_pending = OrderedDict(), set()
        self.hover, self.spin_idx, self.spin_after = None, 0, None
        self.cache_dir, self.job = DEFAULT_CACHE, 0
        self.q = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.blank = tk.PhotoImage(width=THUMB_SIZE, height=THUMB_SIZE)

        top = tk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="Search:").pack(side="left")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.render())
        tk.Entry(top, textvariable=self.query, width=34).pack(side="left", padx=6)
        self.source_filter = tk.StringVar(value=ALL)
        self.source_box = ttk.Combobox(top, textvariable=self.source_filter, state="readonly",
                                       width=36, values=[ALL])
        self.source_box.pack(side="left")
        self.source_box.bind("<<ComboboxSelected>>", lambda e: self.render())
        tk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        tk.Button(top, text="Rescan", command=self.rescan).pack(side="right", padx=6)
        tk.Button(top, text="Library...", command=self.open_library).pack(side="right")

        wrap = tk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0)
        bar = tk.Scrollbar(wrap, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = tk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.status = tk.Label(self, anchor="w")
        self.status.pack(fill="x", padx=8)
        self.summary = ""

        self.after(100, self.poll)
        self.after(150, self.startup)

    # ---- startup / settings / library
    def startup(self):
        if not self.cfg["sources"] or not self.cfg["slicer_path"]:
            messagebox.showinfo("Welcome", f"Let's set things up: add your STL folders or drives, then check the "
                                           f"cache folder and {SLICER_NAME} path in Settings.")
            if not self.cfg["sources"]:
                self.open_library()
            else:
                self.rescan()
                self.open_settings()
        else:
            self.rescan()

    def open_settings(self):
        SettingsDialog(self, self.cfg, self.rescan)

    def open_library(self):
        LibraryDialog(self, self.cfg, self.after_library)

    def after_library(self):
        self.rescan()
        if not self.cfg["slicer_path"]:
            self.open_settings()

    # ---- scanning (background, so big drives don't freeze the window)
    def rescan(self):
        self.scan_id += 1
        sources = list(self.cfg["sources"])
        if not sources:
            self.files, self.file_label, self.missing = [], {}, []
            self.status.config(text="Add folders or drives with the Library button.")
            self.render()
            return
        self.status.config(text="Scanning library...")
        threading.Thread(target=self.scan_task, args=(sources, self.scan_id), daemon=True).start()

    def scan_task(self, sources, sid):
        found, missing = scan_sources(sources)
        self.q.put(("scan", sid, found, missing))

    # ---- grid
    def render(self):
        self.stop_spin()
        self.hover = None
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.buttons.clear()
        self.static.clear()
        self.thumb_refs = []
        q = self.query.get().lower()
        sel = self.source_filter.get()
        shown = [f for f in self.files if q in f.name.lower()
                 and (sel == ALL or self.file_label.get(f) == sel)]
        for i, stl in enumerate(shown):
            cell = tk.Frame(self.grid_frame, padx=6, pady=6)
            cell.grid(row=i // COLUMNS, column=i % COLUMNS)
            btn = tk.Button(cell, image=self.blank, text="...", compound="center",
                            command=lambda s=stl: self.open_slicer(s))
            btn.bind("<Button-3>", lambda e, s=stl: self.show_in_explorer(s))
            btn.bind("<Enter>", lambda e, s=stl: self.on_enter(s))
            btn.bind("<Leave>", lambda e, s=stl: self.on_leave(s))
            btn.pack()
            name = stl.stem if len(stl.stem) < 26 else stl.stem[:23] + "..."
            tk.Label(cell, text=name).pack()
            self.buttons[stl] = btn

        self.cache_dir, warn = usable_cache_dir(self.cfg)
        miss = f"  |  not connected: {', '.join(self.missing)}" if self.missing else ""
        self.summary = f"{len(shown)} models  |  cache: {self.cache_dir} {warn}{miss}"
        self.status.config(text=self.summary)
        self.job += 1
        if shown:
            threading.Thread(target=self.worker, args=(shown, self.cache_dir, self.job), daemon=True).start()

    def worker(self, files, cache_dir, job):
        def task(stl):
            if job == self.job:
                self.q.put(("thumb", job, stl, make_thumb(stl, cache_dir, self.cfg)))
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(task, files))

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "thumb":
                    _, job, stl, png = item
                    btn = self.buttons.get(stl)
                    if job != self.job or btn is None or not btn.winfo_exists():
                        continue
                    if png:
                        img = ImageTk.PhotoImage(Image.open(png))
                        self.static[stl] = img
                        if self.hover != stl:
                            btn.config(image=img, text="")
                    else:
                        self.static[stl] = None
                        if self.hover != stl:
                            btn.config(text="(no preview)")
                elif kind == "spin":
                    _, stl, frames = item
                    self.spin_pending.discard(stl)
                    self.status.config(text=self.summary)
                    if not frames:
                        continue
                    self.spin_cache[stl] = [ImageTk.PhotoImage(f) for f in frames]
                    while len(self.spin_cache) > SPIN_CACHE_MODELS:
                        self.spin_cache.popitem(last=False)
                    if self.hover == stl:
                        self.start_spin()
                elif kind == "scan":
                    _, sid, found, missing = item
                    if sid != self.scan_id:
                        continue
                    found.sort(key=lambda t: t[0].name.lower())
                    self.files = [p for p, _ in found]
                    self.file_label = {p: label for p, label in found}
                    self.missing = missing
                    values = [ALL] + sorted(set(self.file_label.values()))
                    self.source_box.config(values=values)
                    if self.source_filter.get() not in values:
                        self.source_filter.set(ALL)
                    self.render()
        except queue.Empty:
            pass
        self.after(60, self.poll)

    # ---- hover spin
    def on_enter(self, stl):
        self.hover = stl
        if stl in self.spin_cache:
            self.start_spin()
        elif stl not in self.spin_pending:
            self.spin_pending.add(stl)
            self.status.config(text=f"Rendering spin preview for {stl.name} ...")
            self.pool.submit(self.spin_task, stl, self.cache_dir)

    def spin_task(self, stl, cache_dir):
        try:
            frames = load_or_render_spin(stl, cache_dir)
        except Exception:
            frames = None
        self.q.put(("spin", stl, frames))

    def start_spin(self):
        self.stop_spin()
        self.spin_idx = 0
        self.tick()

    def tick(self):
        stl = self.hover
        btn = self.buttons.get(stl)
        if stl is None or stl not in self.spin_cache or btn is None or not btn.winfo_exists():
            return
        frames = self.spin_cache[stl]
        btn.config(image=frames[self.spin_idx % len(frames)], text="")
        self.spin_idx += 1
        self.spin_after = self.after(55, self.tick)

    def stop_spin(self):
        if self.spin_after:
            self.after_cancel(self.spin_after)
            self.spin_after = None

    def on_leave(self, stl):
        if self.hover != stl:
            return
        self.hover = None
        self.stop_spin()
        btn = self.buttons.get(stl)
        if btn is None or not btn.winfo_exists():
            return
        img = self.static.get(stl)
        if img:
            btn.config(image=img, text="")
        else:
            btn.config(image=self.blank, text="(no preview)" if stl in self.static else "...")

    # ---- actions
    def open_slicer(self, stl: Path):
        exe = self.cfg["slicer_path"]
        if not exe or not os.path.exists(exe):
            messagebox.showerror(f"{SLICER_NAME} not found", f"Set the {SLICER_NAME} executable in Settings.")
            return
        subprocess.Popen([exe, str(stl)])

    def show_in_explorer(self, stl: Path):
        subprocess.Popen(["explorer", "/select,", str(stl)])


if __name__ == "__main__":
    App().mainloop()
