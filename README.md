# YT-DLP Downloader

A clean, dark-themed macOS GUI for [yt-dlp](https://github.com/yt-dlp/yt-dlp). Packaged as a standalone `.app` — no Python, Terminal, or admin access required.

## Features

- **Parallel downloads** — add multiple URLs in one window, each with independent config (format, quality, save location)
- **Playlist parallelism** — download up to 4 videos of a playlist at once, with per-video progress, speed, and size
- **New Window** — spawn independent download windows
- **Smoothed stats** — ETA and SI-unit sizes (MB/GB), updated on a 3-second rolling average
- **Resource-friendly** — downloads run at reduced OS priority (`nice 10`) so they don't slow the machine
- **Self-contained** — bundles `yt-dlp`, `ffmpeg`, and `aria2c` inside the `.app`
- **In-app folder browser** — native pickers crash in frozen macOS apps, so a pure-Tk browser is used instead
- **Update button** — pulls the latest `yt-dlp` binary into `~/.ytdlp-downloader/bin/` (no admin needed)

## Run from source

```bash
./run.sh
```

Requires Python 3.13 and `customtkinter` (see `venv/`).

## Build the .app

```bash
source venv/bin/activate
pyinstaller "YT-DLP Downloader.spec" --noconfirm
```

Output: `dist/YT-DLP Downloader.app`.

## Project layout

- `app.py` — the entire application (single source file)
- `YT-DLP Downloader.spec` — PyInstaller build spec
- `run.sh` — launches the app from source
