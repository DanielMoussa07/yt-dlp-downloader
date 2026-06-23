#!/usr/bin/env python3
"""YT-DLP Downloader — multi-slot parallel download GUI."""

import os
import sys

# Must be set before tkinter/customtkinter is imported so filedialog finds its Tcl scripts
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
    os.environ.setdefault("TCL_LIBRARY", os.path.join(_base, "_tcl_data"))
    os.environ.setdefault("TK_LIBRARY",  os.path.join(_base, "_tk_data"))

import customtkinter as ctk
import subprocess
import threading
import urllib.request
import re
import signal
import time
import socket
from pathlib import Path

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# ── Color palette ──────────────────────────────────────────────────────────────
# Explicit everywhere — no CTk theme defaults leak through.

C_WIN      = "#161616"   # window / scrollframe background
C_CARD     = "#212121"   # slot card — visibly elevated above window
C_SECTION  = "#2C2C2C"   # url / options panels — clearly lifted above card
C_INPUT    = "#181818"   # entry fields — recessed / inset
C_BAR      = "#0F0F0F"   # bottom button bar + slot header strip

C_ACCENT   = "#3B82F6"   # primary blue (download)
C_ACCENT_H = "#2563EB"
C_DANGER   = "#EF4444"   # stop / cancel
C_DANGER_H = "#DC2626"
C_GHOST    = "#2C2C2C"   # secondary buttons
C_GHOST_H  = "#3A3A3A"

C_T1       = "#F0F0F0"   # primary text — bright
C_T2       = "#9A9A9E"   # secondary / labels — clearly readable
C_T3       = "#55555A"   # muted / dim
C_T_ACC    = "#60A5FA"   # fetching… / info tint

C_PROG_BG  = "#2C2C2C"   # progress bar track
C_BORDER   = "#363636"   # card border — visible
C_INPUT_BD = "#4A4A4E"   # entry field border — clearly visible

# ── Constants ──────────────────────────────────────────────────────────────────

FORMATS   = ["MP4", "MP3", "MKV", "WebM", "M4A", "Opus"]
QUALITIES = ["Best", "4K (2160p)", "1080p", "720p", "480p", "360p"]
SPEEDS    = ["Normal", "Fast", "Maximum"]
SPEED_DEFAULT    = "Maximum"
PARALLEL_DEFAULT = "4"
DOWNLOAD_DIR     = str(Path.home() / "Downloads")

QUALITY_MAP = {
    "Best":       "bestvideo+bestaudio/best",
    "4K (2160p)": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
    "1080p":      "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p":       "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p":       "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360p":       "bestvideo[height<=360]+bestaudio/best[height<=360]",
}

SPEED_FLAGS = {
    "Normal":  [],
    "Fast":    ["--concurrent-fragments", "8"],
    "Maximum": ["--concurrent-fragments", "16"],
}

SLOT_HEADER_H   = 46
SLOT_EXPANDED_H = 400
MAX_WINDOW_H    = 900
MIN_WINDOW_H    = 520
BTN_BAR_H       = 56
PADDING         = 20
UI_UPDATE_SECS  = 3.0

# ── Single-instance lock ───────────────────────────────────────────────────────

_LOCK_PORT    = 47291
_lock_socket  = None
_NEW_INSTANCE = os.environ.get("YTDLP_NEW_INSTANCE") == "1"


def _acquire_single_instance():
    if _NEW_INSTANCE:
        return True
    global _lock_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", _LOCK_PORT))
        s.listen(1)
        _lock_socket = s
        return True
    except OSError:
        s.close()
        return False


# ── PATH / binary resolution ───────────────────────────────────────────────────

_EXTRA_PATHS = [
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/Library/Frameworks/Python.framework/Versions/3.13/bin",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin",
    "/Library/Frameworks/Python.framework/Versions/3.11/bin",
    str(Path.home() / ".local/bin"),
    str(Path.home() / ".ytdlp-downloader/bin"),
    str(Path.home() / "Library/Python/3.13/bin"),
    str(Path.home() / "Library/Python/3.12/bin"),
]

_ENV = os.environ.copy()

if getattr(sys, "frozen", False):
    _EXTRA_PATHS.insert(0, os.path.join(sys._MEIPASS, "bin"))

_ENV["PATH"] = ":".join(_EXTRA_PATHS) + ":" + _ENV.get("PATH", "")

_KNOWN = {
    "yt-dlp": [
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/yt-dlp",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/yt-dlp",
        "/opt/homebrew/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
        str(Path.home() / ".local/bin/yt-dlp"),
        str(Path.home() / ".ytdlp-downloader/bin/yt-dlp"),
    ],
    "ffmpeg":  ["/opt/homebrew/bin/ffmpeg",  "/usr/local/bin/ffmpeg"],
    "aria2c":  ["/opt/homebrew/bin/aria2c",  "/usr/local/bin/aria2c"],
}


def _find_bin(name):
    if getattr(sys, "frozen", False):
        p = os.path.join(sys._MEIPASS, "bin", name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    user = str(Path.home() / ".ytdlp-downloader" / "bin" / name)
    if os.path.isfile(user) and os.access(user, os.X_OK):
        return user
    for p in _KNOWN.get(name, []):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    r = subprocess.run(["which", name], capture_output=True, text=True, env=_ENV)
    return r.stdout.strip() if r.returncode == 0 else name


# ── Unit helpers ───────────────────────────────────────────────────────────────

_IEC = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
_SI  = {"KB": 1000, "MB": 1_000_000, "GB": 1_000_000_000, "TB": 1_000_000_000_000}
_ALL_UNITS = {**_IEC, **_SI}


def _to_bytes(s):
    m = re.match(r"([\d.]+)(KiB|MiB|GiB|TiB|KB|MB|GB|TB)", s)
    if not m:
        return 0.0
    return float(m.group(1)) * _ALL_UNITS.get(m.group(2), 1)


def _to_si(s):
    b = _to_bytes(s)
    if b <= 0:
        return s
    if b >= 1e9:  return f"{b/1e9:.2f} GB"
    if b >= 1e6:  return f"{b/1e6:.1f} MB"
    return f"{b/1e3:.0f} KB"


def _spd_to_bytes(s):
    m = re.match(r"([\d.]+)(KiB|MiB|GiB|KB|MB|GB)/s", s)
    if not m:
        return 0.0
    return float(m.group(1)) * _ALL_UNITS.get(m.group(2), 1)


def _fmt_eta(secs):
    s = max(0, int(secs))
    if s < 3600:
        return f"{s//60}:{s%60:02d}"
    return f"{s//3600}:{(s%3600)//60:02d}:{s%60:02d}"


# ── FolderBrowser ──────────────────────────────────────────────────────────────

class FolderBrowser(ctk.CTkToplevel):
    """In-app folder picker. A plain Tk window, so it is always visible and
    frontmost — unlike native pickers, which crash or hang in this bundle."""

    HOME = str(Path.home())

    def __init__(self, parent, start_dir, on_select):
        super().__init__(parent)
        self._on_select = on_select
        self._cur = start_dir if os.path.isdir(start_dir) else self.HOME

        self.title("Choose Download Folder")
        self.geometry("560x520")
        self.minsize(440, 380)
        self.configure(fg_color=C_WIN)
        self.transient(parent)
        self.lift()
        self.after(20, self.grab_set)      # modal once mapped
        self.after(30, self.focus_force)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ── Shortcuts row ──────────────────────────────────────────────────────
        shortcuts = ctk.CTkFrame(self, fg_color="transparent")
        shortcuts.grid(row=0, column=0, padx=14, pady=(14, 6), sticky="ew")
        places = [
            ("Home",      self.HOME),
            ("Downloads", os.path.join(self.HOME, "Downloads")),
            ("Desktop",   os.path.join(self.HOME, "Desktop")),
            ("Documents", os.path.join(self.HOME, "Documents")),
            ("Movies",    os.path.join(self.HOME, "Movies")),
        ]
        for name, path in places:
            if os.path.isdir(path):
                ctk.CTkButton(
                    shortcuts, text=name, width=72, height=26,
                    fg_color=C_GHOST, hover_color=C_GHOST_H,
                    text_color=C_T1, font=ctk.CTkFont(size=11),
                    corner_radius=6,
                    command=lambda p=path: self._go(p),
                ).pack(side="left", padx=(0, 6))

        # ── Path bar ───────────────────────────────────────────────────────────
        pathbar = ctk.CTkFrame(self, fg_color=C_SECTION, corner_radius=8)
        pathbar.grid(row=1, column=0, padx=14, pady=6, sticky="ew")
        pathbar.grid_columnconfigure(1, weight=1)

        self._up_btn = ctk.CTkButton(
            pathbar, text="↑ Up", width=56, height=30,
            fg_color=C_GHOST, hover_color=C_GHOST_H,
            text_color=C_T1, font=ctk.CTkFont(size=12),
            corner_radius=6, command=self._up,
        )
        self._up_btn.grid(row=0, column=0, padx=8, pady=8)

        self._path_label = ctk.CTkLabel(
            pathbar, text=self._cur, anchor="w",
            text_color=C_T2, font=ctk.CTkFont(size=12),
        )
        self._path_label.grid(row=0, column=1, padx=(2, 10), pady=8, sticky="ew")

        # ── Folder list ────────────────────────────────────────────────────────
        self._list = ctk.CTkScrollableFrame(
            self, fg_color=C_INPUT, corner_radius=8,
            scrollbar_button_color=C_GHOST,
            scrollbar_button_hover_color=C_GHOST_H,
        )
        self._list.grid(row=2, column=0, padx=14, pady=6, sticky="nsew")
        self._list.grid_columnconfigure(0, weight=1)

        # ── Action row ─────────────────────────────────────────────────────────
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, padx=14, pady=(6, 14), sticky="ew")
        actions.grid_columnconfigure(0, weight=1)

        self._sel_hint = ctk.CTkLabel(
            actions, text="", anchor="w",
            text_color=C_T3, font=ctk.CTkFont(size=11),
        )
        self._sel_hint.grid(row=0, column=0, sticky="ew")

        ctk.CTkButton(
            actions, text="Cancel", width=84, height=34,
            fg_color=C_GHOST, hover_color=C_GHOST_H,
            text_color=C_T2, corner_radius=8,
            command=self.destroy,
        ).grid(row=0, column=1, padx=(0, 6))

        ctk.CTkButton(
            actions, text="Select This Folder", width=150, height=34,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            text_color="#FFFFFF", font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=8, command=self._confirm,
        ).grid(row=0, column=2)

        self.bind("<Escape>", lambda _: self.destroy())
        self._render()

    def _go(self, path):
        if os.path.isdir(path):
            self._cur = os.path.abspath(path)
            self._render()

    def _up(self):
        self._go(os.path.dirname(self._cur))

    def _render(self):
        for w in self._list.winfo_children():
            w.destroy()
        self._path_label.configure(text=self._cur)
        self._up_btn.configure(state="normal" if self._cur != "/" else "disabled")

        try:
            entries = sorted(
                (e for e in os.listdir(self._cur)
                 if not e.startswith(".")
                 and os.path.isdir(os.path.join(self._cur, e))),
                key=str.lower,
            )
        except PermissionError:
            ctk.CTkLabel(
                self._list, text="Permission denied for this folder.",
                text_color=C_DANGER, font=ctk.CTkFont(size=12),
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return
        except OSError as e:
            ctk.CTkLabel(
                self._list, text=f"Cannot open: {e}",
                text_color=C_DANGER, font=ctk.CTkFont(size=12),
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return

        if not entries:
            ctk.CTkLabel(
                self._list, text="(no sub-folders here — click “Select This Folder”)",
                text_color=C_T3, font=ctk.CTkFont(size=12),
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return

        for i, name in enumerate(entries):
            full = os.path.join(self._cur, name)
            ctk.CTkButton(
                self._list, text=f"  📁  {name}", anchor="w",
                height=30, fg_color="transparent",
                hover_color=C_GHOST_H, text_color=C_T1,
                font=ctk.CTkFont(size=12), corner_radius=6,
                command=lambda p=full: self._go(p),
            ).grid(row=i, column=0, padx=4, pady=1, sticky="ew")

    def _confirm(self):
        self.destroy()
        self._on_select(self._cur)


# ── DownloadSlot ───────────────────────────────────────────────────────────────

class DownloadSlot:
    def __init__(self, app, container, slot_id):
        self._app       = app
        self._container = container
        self._slot_id   = slot_id
        self._collapsed = False

        self._process        = None
        self._processes      = []
        self._stop_requested = False

        self._fetch_gen      = 0
        self._fetch_after_id = None

        self._speed_samples = []
        self._last_ui_upd   = 0.0
        self._last_pct      = 0.0
        self._total_bytes   = 0.0

        self._pl_total      = 0
        self._pl_ok         = 0      # videos that finished with rc == 0
        self._pl_fail       = 0      # videos that errored (rc != 0)
        self._pl_active     = 0
        self._pl_lock       = threading.Lock()
        self._current_title = ""
        self._active        = {}     # idx -> {title, pct, size, speed} for live per-video stats
        self._pl_render_id  = None   # pending after() id for the periodic renderer

        self.format_var   = ctk.StringVar(value="MP4")
        self.quality_var  = ctk.StringVar(value="Best")
        self.playlist_var = ctk.BooleanVar(value=False)
        self.speed_var    = ctk.StringVar(value=SPEED_DEFAULT)
        self.parallel_var = ctk.StringVar(value=PARALLEL_DEFAULT)
        self._dir_path    = DOWNLOAD_DIR

        self.slot_frame        = None
        self.header_frame      = None
        self.body_frame        = None
        self.url_entry         = None
        self.title_label       = None
        self.format_menu       = None
        self.quality_menu      = None
        self.playlist_check    = None
        self.parallel_frame    = None
        self.speed_menu        = None
        self.dir_label         = None
        self.progress_bar      = None
        self.status_label      = None
        self.spd_label         = None
        self.detail_label      = None
        self.stop_btn          = None
        self.collapse_btn      = None
        self.remove_btn        = None
        self.header_info_label = None

    # ── Build ──────────────────────────────────────────────────────────────────

    def build(self, row):
        self.slot_frame = ctk.CTkFrame(
            self._container,
            fg_color=C_CARD,
            corner_radius=12,
            border_width=1,
            border_color=C_BORDER,
        )
        self.slot_frame.grid(row=row, column=0, padx=10, pady=(0, 8), sticky="ew")
        self.slot_frame.grid_columnconfigure(0, weight=1)
        self._build_header()
        self._build_body()

    def _build_header(self):
        self.header_frame = ctk.CTkFrame(
            self.slot_frame,
            fg_color=C_BAR,
            corner_radius=10,
            height=SLOT_HEADER_H,
        )
        self.header_frame.grid(row=0, column=0, padx=3, pady=(3, 0), sticky="ew")
        self.header_frame.grid_columnconfigure(1, weight=1)
        self.header_frame.grid_propagate(False)

        self.remove_btn = ctk.CTkButton(
            self.header_frame, text="×", width=28, height=28,
            fg_color="transparent", hover_color=C_GHOST_H,
            text_color=C_T3, font=ctk.CTkFont(size=18),
            command=lambda: self._app._remove_slot(self),
        )
        self.remove_btn.grid(row=0, column=0, padx=(6, 2), pady=9)

        self.header_info_label = ctk.CTkLabel(
            self.header_frame, text="New download",
            anchor="w", text_color=C_T2,
            font=ctk.CTkFont(size=12),
        )
        self.header_info_label.grid(row=0, column=1, padx=4, pady=9, sticky="ew")

        self.collapse_btn = ctk.CTkButton(
            self.header_frame, text="▼", width=28, height=28,
            fg_color="transparent", hover_color=C_GHOST_H,
            text_color=C_T3, font=ctk.CTkFont(size=11),
            command=self._toggle_collapse,
        )
        self.collapse_btn.grid(row=0, column=2, padx=(2, 6), pady=9)

        self.header_frame.grid_remove()

    def _build_body(self):
        self.body_frame = ctk.CTkFrame(self.slot_frame, fg_color="transparent")
        self.body_frame.grid(row=1, column=0, sticky="ew")
        self.body_frame.grid_columnconfigure(0, weight=1)

        # ── URL section ────────────────────────────────────────────────────────
        url_frame = ctk.CTkFrame(
            self.body_frame, fg_color=C_SECTION, corner_radius=8,
        )
        url_frame.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="ew")
        url_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            url_frame, text="URL", anchor="w",
            text_color=C_T3, font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(10, 3), sticky="w")

        self.url_entry = ctk.CTkEntry(
            url_frame,
            placeholder_text="Paste a YouTube, Vimeo, or other URL…",
            height=38, border_width=1,
            border_color=C_INPUT_BD,
            fg_color=C_INPUT,
            text_color=C_T1,
            placeholder_text_color=C_T3,
        )
        self.url_entry.grid(row=1, column=0, padx=10, pady=(0, 4), sticky="ew")
        self.url_entry.bind("<Return>",     lambda _: self._app._start_all())
        self.url_entry.bind("<<Paste>>",    lambda _: self._app.after(50, self._on_url_change))
        self.url_entry.bind("<KeyRelease>", lambda _: self._schedule_fetch())

        self.title_label = ctk.CTkLabel(
            url_frame, text="", anchor="w",
            text_color=C_T1, font=ctk.CTkFont(size=12, weight="bold"),
            wraplength=420,
        )
        self.title_label.grid(row=2, column=0, padx=14, pady=(2, 10), sticky="w")

        # ── Options section ────────────────────────────────────────────────────
        opt = ctk.CTkFrame(self.body_frame, fg_color=C_SECTION, corner_radius=8)
        opt.grid(row=1, column=0, padx=8, pady=4, sticky="ew")
        opt.grid_columnconfigure(1, weight=1)

        _FONT_LBL = ctk.CTkFont(size=12)
        _FONT_SML = ctk.CTkFont(size=11)

        def _row_label(text, r, pady=(0, 2)):
            ctk.CTkLabel(
                opt, text=text, width=80, anchor="e",
                text_color=C_T2, font=_FONT_LBL,
            ).grid(row=r * 2, column=0, padx=(14, 10), pady=(10, 0), sticky="e")

        def _menu(parent, values, variable, width=190):
            return ctk.CTkOptionMenu(
                parent, values=values, variable=variable, width=width,
                fg_color=C_INPUT, button_color=C_GHOST_H,
                button_hover_color=C_T3,
                dropdown_fg_color=C_INPUT,
                dropdown_hover_color=C_GHOST_H,
                text_color=C_T1,
                dropdown_text_color=C_T1,
                font=_FONT_LBL,
            )

        _row_label("Format",   0)
        self.format_menu = _menu(opt, FORMATS, self.format_var)
        self.format_menu.grid(row=0, column=1, padx=(0, 14), pady=(10, 0), sticky="w")

        _row_label("Quality",  1)
        self.quality_menu = _menu(opt, QUALITIES, self.quality_var)
        self.quality_menu.grid(row=2, column=1, padx=(0, 14), pady=(10, 0), sticky="w")

        _row_label("Speed",    2)
        self.speed_menu = _menu(opt, SPEEDS, self.speed_var)
        self.speed_menu.grid(row=4, column=1, padx=(0, 14), pady=(10, 0), sticky="w")

        _row_label("Playlist", 3)
        pl_row = ctk.CTkFrame(opt, fg_color="transparent")
        pl_row.grid(row=6, column=1, padx=(0, 14), pady=(10, 0), sticky="w")

        self.playlist_check = ctk.CTkCheckBox(
            pl_row, text="Download entire playlist",
            variable=self.playlist_var,
            checkbox_width=16, checkbox_height=16,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            border_color=C_T3, text_color=C_T1,
            font=_FONT_LBL,
            command=self._on_playlist_toggle,
        )
        self.playlist_check.pack(side="left")

        self.parallel_frame = ctk.CTkFrame(pl_row, fg_color="transparent")
        ctk.CTkLabel(
            self.parallel_frame, text="  Parallel:",
            text_color=C_T2, font=_FONT_SML,
        ).pack(side="left")
        ctk.CTkOptionMenu(
            self.parallel_frame,
            values=["1", "2", "3", "4"],
            variable=self.parallel_var,
            width=62, font=_FONT_SML,
            fg_color=C_INPUT, button_color=C_GHOST_H,
            button_hover_color=C_T3,
            dropdown_fg_color=C_INPUT,
            dropdown_hover_color=C_GHOST_H,
            text_color=C_T1,
            dropdown_text_color=C_T1,
        ).pack(side="left", padx=(4, 0))

        _row_label("Save to",  4)
        dir_row = ctk.CTkFrame(opt, fg_color="transparent")
        dir_row.grid(row=8, column=1, padx=(0, 14), pady=(10, 12), sticky="ew")
        dir_row.grid_columnconfigure(0, weight=1)

        self.dir_label = ctk.CTkLabel(
            dir_row, text=self._dir_path, anchor="w",
            text_color=C_T2, font=ctk.CTkFont(size=11),
        )
        self.dir_label.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            dir_row, text="Change", width=60, height=22,
            fg_color=C_GHOST, hover_color=C_GHOST_H,
            text_color=C_T2, font=ctk.CTkFont(size=11),
            corner_radius=6,
            command=self._choose_dir,
        ).grid(row=0, column=1, padx=(8, 0))

        # ── Progress bar ───────────────────────────────────────────────────────
        self.progress_bar = ctk.CTkProgressBar(
            self.body_frame,
            height=4, corner_radius=2,
            fg_color=C_PROG_BG,
            progress_color=C_ACCENT,
        )
        self.progress_bar.grid(row=2, column=0, padx=8, pady=(4, 0), sticky="ew")
        self.progress_bar.set(0)

        # ── Status strip (single-line header: status • speed • Stop) ───────────
        sf_inner = ctk.CTkFrame(
            self.body_frame, fg_color=C_BAR, corner_radius=10, height=34,
        )
        sf_inner.grid(row=3, column=0, sticky="ew", padx=0, pady=0)
        sf_inner.grid_columnconfigure(0, weight=1)
        sf_inner.grid_propagate(False)

        self.status_label = ctk.CTkLabel(
            sf_inner, text="Ready", anchor="w",
            text_color=C_T2, font=ctk.CTkFont(size=11),
        )
        self.status_label.grid(row=0, column=0, padx=14, pady=7, sticky="w")

        self.spd_label = ctk.CTkLabel(
            sf_inner, text="", anchor="e",
            text_color=C_T2, font=ctk.CTkFont(size=11),
        )
        self.spd_label.grid(row=0, column=1, padx=(0, 6), pady=7)

        self.stop_btn = ctk.CTkButton(
            sf_inner, text="Stop", width=52, height=22,
            fg_color=C_DANGER, hover_color=C_DANGER_H,
            text_color=C_T1, font=ctk.CTkFont(size=11, weight="bold"),
            corner_radius=6,
            command=self.stop_download,
        )
        self.stop_btn.grid(row=0, column=2, padx=(0, 8), pady=6)
        self.stop_btn.grid_remove()

        # ── Per-video detail rows (parallel playlist mode) ─────────────────────
        self.detail_label = ctk.CTkLabel(
            self.body_frame, text="", anchor="w", justify="left",
            text_color=C_T2, font=ctk.CTkFont(size=11),
        )
        self.detail_label.grid(row=4, column=0, padx=16, pady=(2, 6), sticky="ew")
        self.detail_label.grid_remove()

    # ── Collapse ───────────────────────────────────────────────────────────────

    def _toggle_collapse(self):
        self.set_collapsed(not self._collapsed)
        self._app._update_window_height()

    def set_collapsed(self, v):
        self._collapsed = v
        if v:
            self.body_frame.grid_remove()
            self.collapse_btn.configure(text="▶")
        else:
            self.body_frame.grid()
            self.collapse_btn.configure(text="▼")

    def set_header_visible(self, v):
        if v: self.header_frame.grid()
        else: self.header_frame.grid_remove()

    def set_remove_visible(self, v):
        if v: self.remove_btn.grid()
        else: self.remove_btn.grid_remove()

    def destroy_widgets(self):
        if self._fetch_after_id:
            try: self._app.after_cancel(self._fetch_after_id)
            except Exception: pass
        self.slot_frame.destroy()

    # ── Playlist toggle ────────────────────────────────────────────────────────

    def _on_playlist_toggle(self):
        if self.playlist_var.get():
            self.parallel_frame.pack(side="left")
        else:
            self.parallel_frame.pack_forget()
        # Re-evaluate the title preview so the no-playlist warning appears/clears.
        if self.url_entry.get().strip().startswith("http"):
            self._on_url_change()

    # ── Title fetch ────────────────────────────────────────────────────────────

    def _schedule_fetch(self):
        if self._fetch_after_id:
            self._app.after_cancel(self._fetch_after_id)
        self._fetch_after_id = self._app.after(600, self._on_url_change)

    def _on_url_change(self):
        self._fetch_after_id = None
        url = self.url_entry.get().strip()
        if not url or not url.startswith("http"):
            self.title_label.configure(text="")
            self.header_info_label.configure(text="New download")
            return
        self._fetch_gen += 1
        gen = self._fetch_gen
        self.title_label.configure(text="Fetching title…", text_color=C_T_ACC)
        threading.Thread(target=self._fetch_title, args=(url, gen), daemon=True).start()

    def _fetch_title(self, url, gen):
        ytdlp = _find_bin("yt-dlp")
        try:
            r = subprocess.run(
                [ytdlp, "--flat-playlist", "--playlist-items", "1",
                 "--print", "%(playlist_title)s\t%(playlist_count)s\t%(title)s",
                 "--no-warnings", url],
                capture_output=True, text=True, env=_ENV, timeout=45,
            )
            ok  = r.returncode == 0 and r.stdout.strip()
            out = r.stdout.strip() if ok else ""
        except Exception:
            out = ""

        if gen != self._fetch_gen:
            return

        if not out:
            self._app.after(0, lambda: self.title_label.configure(
                text="Could not fetch title.", text_color=C_T3))
            return

        parts    = out.splitlines()[0].split("\t")
        pl_title = parts[0].strip() if len(parts) > 0 else ""
        pl_count = parts[1].strip() if len(parts) > 1 else ""
        v_title  = parts[2].strip() if len(parts) > 2 else ""

        is_pl    = pl_title and pl_title.lower() not in ("na", "none", "") and pl_title != v_title
        has_list = "list=" in url
        if is_pl:
            cnt     = f" ({pl_count} videos)" if pl_count not in ("NA", "None", "") else ""
            display = f"Playlist: {pl_title}{cnt}"
        else:
            display = v_title

        # Warn when "Download entire playlist" is on but the URL points to a single
        # video with no &list=… — yt-dlp has no playlist to expand in that case.
        warn = (self.playlist_var.get() and not is_pl and not has_list)

        def _upd():
            self.title_label.configure(text=display, text_color=C_T1)
            short = display[:52] + ("…" if len(display) > 52 else "")
            self.header_info_label.configure(text=short)
            if warn:
                self.title_label.configure(
                    text=display + "\n⚠ No playlist in this URL — only this one"
                         " video will download. Copy the link with “&list=…” in it"
                         " to get the whole series.",
                    text_color="#E8A33D",
                )
        self._app.after(0, _upd)

    # ── Download state ─────────────────────────────────────────────────────────

    def is_downloading(self):
        if self._processes:
            return any(p.poll() is None for p in self._processes)
        return self._process is not None and self._process.poll() is None

    def start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            return
        self._stop_requested = False
        self._speed_samples  = []
        self._last_ui_upd    = 0.0
        self._last_pct       = 0.0
        self._pl_total       = 0
        self._pl_ok          = 0
        self._pl_fail        = 0
        self._pl_active      = 0
        self._active         = {}
        self._processes      = []
        self._current_title  = ""
        self._set_busy()

        if self.playlist_var.get():
            threading.Thread(target=self._run_playlist_parallel, args=(url,), daemon=True).start()
        else:
            threading.Thread(target=self._run_download, args=(self._build_cmd(url),), daemon=True).start()

    def _set_busy(self):
        self.progress_bar.set(0)
        self._set_status("Starting…")
        for w in (self.format_menu, self.quality_menu, self.playlist_check, self.speed_menu):
            w.configure(state="disabled")
        self.stop_btn.grid()

    def _set_idle(self, status="Ready"):
        self._process   = None
        self._processes = []
        for w in (self.format_menu, self.quality_menu, self.playlist_check, self.speed_menu):
            w.configure(state="normal")
        self.stop_btn.grid_remove()
        if self.detail_label is not None:
            self.detail_label.configure(text="")
            self.detail_label.grid_remove()
        self._set_status(status)
        self._app._on_slot_state_change()

    def stop_download(self):
        self._stop_requested = True
        for p in list(self._processes):
            try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                try: p.terminate()
                except Exception: pass
        if self._process:
            try: os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            except Exception:
                try: self._process.terminate()
                except Exception: pass
        self._app.after(0, lambda: self._set_idle("Cancelled."))

    def _set_status(self, text, speed=""):
        self.status_label.configure(text=text)
        self.spd_label.configure(text=speed)

    def _choose_dir(self):
        # In-app folder browser. Native pickers are unusable in this bundle:
        #  * tkinter.filedialog.askdirectory() CRASHES the frozen app on macOS
        #    (bugs.python.org/issue44828, pyinstaller#4334).
        #  * PyObjC NSOpenPanel CRASHES — fights Tk for the shared NSApplication.
        #  * osascript `choose folder` is a faceless subprocess: its window never
        #    comes to the front, so the dialog is invisible and hangs forever.
        # A plain Tk Toplevel window has none of these problems — it's part of our
        # own visible app, no subprocess, no permissions.
        FolderBrowser(self._app, self._dir_path, self._on_dir_chosen)

    def _on_dir_chosen(self, path):
        self._dir_path = path
        self.dir_label.configure(text=path)

    # ── Command building ───────────────────────────────────────────────────────

    def _build_cmd(self, url, playlist_item=None):
        fmt      = self.format_var.get().lower()
        quality  = self.quality_var.get()
        ytdlp    = _find_bin("yt-dlp")
        ffmpeg   = _find_bin("ffmpeg")
        aria2c   = _find_bin("aria2c")
        out_tmpl = os.path.join(self._dir_path, "%(title)s.%(ext)s")

        cmd = [ytdlp, "--ffmpeg-location", ffmpeg]

        if fmt in ("mp3", "m4a", "opus"):
            cmd += ["-x", "--audio-format", fmt, "--audio-quality", "0"]
        else:
            fmt_str = QUALITY_MAP.get(quality, "bestvideo+bestaudio/best")
            merge   = fmt if fmt in ("mp4", "mkv", "webm") else "mp4"
            cmd += ["-f", fmt_str, "--merge-output-format", merge, "--remux-video", merge]

        speed = self.speed_var.get()
        cmd  += SPEED_FLAGS.get(speed, [])

        # NOTE: aria2c is intentionally NOT used. On YouTube, aria2c's many parallel
        # connections get throttled to ~0 KB/s under load and leave 0-byte .part
        # files. yt-dlp's native downloader rides out YouTube's nsig throttle and
        # ramps back up, so --concurrent-fragments (the SPEED_FLAGS) is the fast,
        # reliable path. `aria2c` is kept resolved above only for compatibility.
        _ = aria2c

        cmd += ["-o", out_tmpl]

        # Survive YouTube throttling, esp. under parallel load: retry the whole
        # download and individual fragments instead of erroring out.
        cmd += ["--retries", "5", "--fragment-retries", "20",
                "--retry-sleep", "3", "--no-update"]

        if playlist_item is not None:
            cmd += ["--playlist-items", str(playlist_item)]
        elif not self.playlist_var.get():
            cmd.append("--no-playlist")

        cmd += ["--newline", "--progress", url]
        return cmd

    # ── Single-video download ──────────────────────────────────────────────────

    def _run_download(self, cmd):
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True, env=_ENV,
                preexec_fn=lambda: os.nice(10),
            )
            for line in self._process.stdout:
                self._parse_progress(line.strip(), pl_idx=None, pl_total=None)
            self._process.wait()
            rc = self._process.returncode
            if self._stop_requested:
                return
            if rc == 0:
                self._app.after(0, lambda: self.progress_bar.set(1.0))
                self._app.after(0, lambda: self._set_idle("Done."))
            elif rc is not None:
                self._app.after(0, lambda: self._set_idle(f"Error (exit {rc}). Check URL/format."))
        except Exception as e:
            self._app.after(0, lambda err=e: self._set_idle(f"Error: {err}"))

    # ── Playlist parallel ──────────────────────────────────────────────────────

    def _get_playlist_ids(self, url):
        ytdlp = _find_bin("yt-dlp")
        try:
            r = subprocess.run(
                [ytdlp, "--flat-playlist", "--print", "id", "--no-warnings", url],
                capture_output=True, text=True, env=_ENV, timeout=30,
            )
            if r.returncode != 0:
                return []
            return [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
        except Exception:
            return []

    def _run_playlist_parallel(self, url):
        self._app.after(0, lambda: self._set_status("Fetching playlist…"))
        ids = self._get_playlist_ids(url)
        if not ids:
            self._app.after(0, lambda: self._set_idle("Could not fetch playlist."))
            return

        n = int(self.parallel_var.get())
        self._pl_total = len(ids)
        self._pl_ok    = 0
        self._pl_fail  = 0
        self._app.after(0, lambda: self._set_status(f"0/{self._pl_total} videos done"))

        sem     = threading.Semaphore(n)
        threads = [
            threading.Thread(
                target=self._dl_one,
                args=(url, i + 1, len(ids), sem),
                daemon=True,
            )
            for i in range(len(ids))
        ]
        # Kick off the periodic UI renderer on the main thread.
        self._app.after(0, self._render_pl)
        for t in threads: t.start()
        for t in threads: t.join()

        if self._stop_requested:
            return
        ok, fail, total = self._pl_ok, self._pl_fail, self._pl_total
        self._app.after(0, lambda: self.progress_bar.set(1.0))
        if fail:
            msg = f"Done — {ok}/{total} saved, {fail} failed (try Update yt-dlp, then re-run)."
        else:
            msg = f"Done. All {total} videos saved."
        self._app.after(0, lambda m=msg: self._set_idle(m))

    def _render_pl(self):
        # Runs on the Tk main thread; repaints aggregate counter + per-video rows
        # every UI tick while the playlist is downloading.
        with self._pl_lock:
            ok, fail, total = self._pl_ok, self._pl_fail, self._pl_total
            active = {i: dict(e) for i, e in self._active.items()}

        pct  = (ok + fail) / total if total else 0
        head = f"{ok}/{total} videos done"
        if active:
            head += f"  •  {len(active)} downloading"
        if fail:
            head += f"  •  {fail} failed"

        lines = []
        for idx in sorted(active):
            e = active[idx]
            t = (e.get("title") or f"item {idx}")[:34]
            if "pct" in e:
                lines.append(f"↓ {t} — {e['pct']:.0f}%  •  {e.get('size','')}  •  {e.get('speed','')}")
            else:
                lines.append(f"↓ {t} — starting…")

        self.progress_bar.set(pct)
        self.status_label.configure(text=head)
        self.spd_label.configure(text="")
        if lines:
            self.detail_label.configure(text="\n".join(lines))
            self.detail_label.grid()
        else:
            self.detail_label.grid_remove()

        if self.is_downloading():
            self._pl_render_id = self._app.after(1500, self._render_pl)
        else:
            self._pl_render_id = None

    def _dl_one(self, url, idx, total, sem):
        with sem:
            if self._stop_requested:
                return
            with self._pl_lock:
                self._pl_active += 1
                self._active.setdefault(idx, {})
            cmd = self._build_cmd(url, playlist_item=idx)
            rc = -1
            try:
                p = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True, env=_ENV,
                    preexec_fn=lambda: os.nice(10),
                )
                with self._pl_lock:
                    self._processes.append(p)
                for line in p.stdout:
                    self._parse_progress(line.strip(), pl_idx=idx, pl_total=total)
                p.wait()
                rc = p.returncode
            except Exception:
                rc = -1
            finally:
                with self._pl_lock:
                    self._pl_active = max(0, self._pl_active - 1)
                    if not self._stop_requested:
                        if rc == 0:
                            self._pl_ok += 1
                        else:
                            self._pl_fail += 1
                    self._active.pop(idx, None)

    # ── Progress parsing ───────────────────────────────────────────────────────

    def _parse_progress(self, line, pl_idx, pl_total):
        parallel = pl_idx is not None

        # In parallel playlist mode each child runs `--playlist-items N`. Stash its
        # title + live %/size/speed into self._active[idx]; the periodic renderer
        # (_render_pl) paints the aggregate counter + one detail line per video.
        if parallel:
            m_dest = re.search(r"\[download\] Destination: (.+)", line)
            if m_dest:
                name = os.path.splitext(os.path.basename(m_dest.group(1)))[0]
                name = re.sub(r"\.f\d+$", "", name)   # drop yt-dlp format-code suffix
                with self._pl_lock:
                    self._active.setdefault(pl_idx, {})["title"] = name
                return
            m = re.search(
                r"\[download\]\s+([\d.]+)%\s+of\s+(\S+)\s+at\s+(\S+/s)", line)
            if m:
                with self._pl_lock:
                    e = self._active.setdefault(pl_idx, {})
                    e["pct"]   = float(m.group(1))
                    e["size"]  = _to_si(m.group(2))
                    e["speed"] = _to_si(m.group(3).replace("/s", "")) + "/s"
                return
            return

        m = re.search(
            r"\[download\]\s+([\d.]+)%\s+of\s+(\S+)\s+at\s+(\S+/s)\s+ETA\s+\S+",
            line,
        )
        if m:
            pct_raw   = float(m.group(1)) / 100
            size_raw  = m.group(2)
            speed_raw = m.group(3)

            bps = _spd_to_bytes(speed_raw)
            self._speed_samples.append(bps)
            if len(self._speed_samples) > 12:
                self._speed_samples.pop(0)
            self._last_pct    = pct_raw
            self._total_bytes = _to_bytes(size_raw)

            now = time.monotonic()
            if now - self._last_ui_upd >= UI_UPDATE_SECS:
                self._last_ui_upd = now
                avg_bps   = sum(self._speed_samples) / len(self._speed_samples) if self._speed_samples else 0
                remaining = self._total_bytes * (1 - pct_raw)
                eta_secs  = remaining / avg_bps if avg_bps > 0 else 0
                size_si   = _to_si(size_raw)
                spd_si    = _to_si(speed_raw.replace("/s", "")) + "/s"
                eta_str   = _fmt_eta(eta_secs)
                title     = self._current_title

                prefix = f"({pl_idx}/{pl_total}) " if pl_idx and pl_total else ""
                status = (f"{prefix}↓ {title[:36]}  •  {size_si}  •  ETA {eta_str}"
                          if title else
                          f"{prefix}{size_si}  •  ETA {eta_str}")

                def _upd(p=pct_raw, s=status, sp=spd_si):
                    if pl_idx is None:
                        self.progress_bar.set(p)
                    self._set_status(s, sp)
                self._app.after(0, _upd)
            return

        m_dest = re.search(r"\[download\] Destination: (.+)", line)
        if m_dest:
            name = os.path.splitext(os.path.basename(m_dest.group(1)))[0]
            self._current_title = name
            if pl_idx is None:
                self._app.after(0, lambda n=name: self._set_status(f"↓ {n}"))
            return

        m_pl = re.search(r"\[download\] Downloading item (\d+) of (\d+)", line)
        if m_pl:
            i_, t_ = int(m_pl.group(1)), int(m_pl.group(2))
            pct = i_ / t_
            self._app.after(0, lambda p=pct: self.progress_bar.set(p))
            self._app.after(0, lambda i=i_, t=t_: self._set_status(f"Video {i}/{t}"))
            return

        if "[Merger]" in line or "Merging formats" in line:
            self._app.after(0, lambda: self._set_status("Merging…"))
            return
        if "[ffmpeg]" in line:
            self._app.after(0, lambda: self._set_status("Processing…"))


# ── App ────────────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("YT-DLP Downloader")
        self.configure(fg_color=C_WIN)
        self.geometry("520x560")
        self.resizable(True, True)          # fully resizable — drag any edge
        self.minsize(420, 360)

        self._user_resized = False
        self._auto_geom    = ""
        self.bind("<Configure>", self._on_configure)

        self._slots        = []
        self._slot_counter = 0
        self._build_ui()
        self._add_slot()

    def _on_configure(self, event):
        # Detect a manual resize: a <Configure> on the root whose geometry differs
        # from the last size we set programmatically. Once the user resizes, stop
        # auto-managing the height so we never fight their chosen size.
        if event.widget is self:
            g = f"{self.winfo_width()}x{self.winfo_height()}"
            if self._auto_geom and g != self._auto_geom:
                self._user_resized = True

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._slots_frame = ctk.CTkScrollableFrame(
            self,
            corner_radius=0,
            fg_color=C_WIN,
            scrollbar_button_color=C_GHOST,
            scrollbar_button_hover_color=C_GHOST_H,
        )
        self._slots_frame.grid(row=0, column=0, padx=0, pady=(8, 0), sticky="nsew")
        self._slots_frame.grid_columnconfigure(0, weight=1)

        # ── Bottom bar ─────────────────────────────────────────────────────────
        bar = ctk.CTkFrame(
            self,
            fg_color=C_BAR,
            corner_radius=0,
            height=BTN_BAR_H,
        )
        bar.grid(row=1, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        bar.grid_propagate(False)

        _BTN_FONT = ctk.CTkFont(size=12)

        self._update_btn = ctk.CTkButton(
            bar, text="Update yt-dlp", width=118, height=32,
            fg_color=C_GHOST, hover_color=C_GHOST_H,
            text_color=C_T2, font=_BTN_FONT,
            corner_radius=8,
            command=self._update_ytdlp,
        )
        self._update_btn.grid(row=0, column=0, padx=(12, 6), pady=12)

        self._add_btn = ctk.CTkButton(
            bar, text="+ Add URL", width=84, height=32,
            fg_color=C_GHOST, hover_color=C_GHOST_H,
            text_color=C_T2, font=_BTN_FONT,
            corner_radius=8,
            command=self._add_slot,
        )
        self._add_btn.grid(row=0, column=1, padx=6, pady=12, sticky="w")

        self._dl_btn = ctk.CTkButton(
            bar, text="Download", width=118, height=32,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            text_color="#FFFFFF",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=8,
            command=self._toggle_all,
        )
        self._dl_btn.grid(row=0, column=2, padx=6, pady=12)

        self._new_win_btn = ctk.CTkButton(
            bar, text="New Window", width=106, height=32,
            fg_color=C_GHOST, hover_color=C_GHOST_H,
            text_color=C_T2, font=_BTN_FONT,
            corner_radius=8,
            command=self._spawn_new_window,
        )
        self._new_win_btn.grid(row=0, column=3, padx=(6, 12), pady=12)
        self._new_win_btn.grid_remove()

    # ── Slot management ────────────────────────────────────────────────────────

    def _add_slot(self):
        slot = DownloadSlot(
            app=self, container=self._slots_frame, slot_id=self._slot_counter
        )
        self._slot_counter += 1
        self._slots.append(slot)
        slot.build(row=len(self._slots) - 1)
        self._reflow()

    def _remove_slot(self, slot):
        if len(self._slots) <= 1:
            return
        if slot.is_downloading():
            slot.stop_download()
        self._slots.remove(slot)
        slot.destroy_widgets()
        for i, s in enumerate(self._slots):
            s.slot_frame.grid(row=i, column=0, padx=10, pady=(0, 8), sticky="ew")
        self._reflow()

    def _reflow(self):
        single = len(self._slots) == 1
        for s in self._slots:
            s.set_header_visible(not single)
            s.set_remove_visible(not single)
            if single and s._collapsed:
                s.set_collapsed(False)
        self._update_window_height()

    def _update_window_height(self):
        # Auto-size height to fit the slots — but only until the user manually
        # resizes the window, after which we leave their size alone. Width is
        # always preserved (never forced).
        if self._user_resized:
            return
        total = sum(
            SLOT_HEADER_H if s._collapsed else SLOT_EXPANDED_H
            for s in self._slots
        )
        h = max(MIN_WINDOW_H, min(MAX_WINDOW_H, total + BTN_BAR_H + PADDING))
        self.update_idletasks()
        w = self.winfo_width() if self.winfo_width() > 1 else 520
        self._auto_geom = f"{w}x{h}"
        self.geometry(self._auto_geom)

    # ── Download orchestration ─────────────────────────────────────────────────

    def _toggle_all(self):
        if any(s.is_downloading() for s in self._slots):
            for s in self._slots:
                if s.is_downloading():
                    s.stop_download()
        else:
            self._start_all()

    def _start_all(self):
        active = [s for s in self._slots if s.url_entry.get().strip()]
        if not active:
            return
        for s in active:
            if not s.is_downloading():
                s.start_download()
        self._on_slot_state_change()

    def _on_slot_state_change(self):
        downloading = any(s.is_downloading() for s in self._slots)
        if downloading:
            self._dl_btn.configure(
                text="Stop All",
                fg_color=C_DANGER, hover_color=C_DANGER_H,
            )
            self._new_win_btn.grid()
        else:
            self._dl_btn.configure(
                text="Download",
                fg_color=C_ACCENT, hover_color=C_ACCENT_H,
            )
            self._new_win_btn.grid_remove()

    # ── New window ─────────────────────────────────────────────────────────────

    def _spawn_new_window(self):
        env = os.environ.copy()
        env["YTDLP_NEW_INSTANCE"] = "1"
        exe = "/Applications/YT-DLP Downloader.app/Contents/MacOS/YT-DLP Downloader"
        if os.path.isfile(exe):
            subprocess.Popen([exe], env=env, start_new_session=True)
        else:
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__)],
                env=env, start_new_session=True,
            )

    # ── Update yt-dlp ──────────────────────────────────────────────────────────

    def _update_ytdlp(self):
        self._update_btn.configure(state="disabled", text="Updating…")
        if self._slots:
            self._slots[0]._set_status("Updating yt-dlp…")

        def run():
            if getattr(sys, "frozen", False):
                msg = self._dl_ytdlp_binary()
            else:
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                    capture_output=True, text=True, env=_ENV,
                )
                if r.returncode == 0:
                    m   = re.search(r"Successfully installed yt-dlp-([\d.]+)", r.stdout)
                    msg = f"Updated to yt-dlp {m.group(1)}" if m else "yt-dlp already up to date."
                else:
                    lines = (r.stderr or r.stdout).strip().splitlines()
                    msg   = lines[-1] if lines else "Update failed."

            def done():
                self._update_btn.configure(state="normal", text="Update yt-dlp")
                if self._slots:
                    self._slots[0]._set_status(msg)
            self.after(0, done)

        threading.Thread(target=run, daemon=True).start()

    def _dl_ytdlp_binary(self):
        bin_dir = Path.home() / ".ytdlp-downloader" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        dest = bin_dir / "yt-dlp"
        url  = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
        try:
            urllib.request.urlretrieve(url, str(dest))
            os.chmod(dest, 0o755)
            return f"Updated yt-dlp → {dest}"
        except Exception as e:
            return f"Update failed: {e}"


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _acquire_single_instance():
        sys.exit(0)
    app = App()
    app.mainloop()
