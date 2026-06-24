# -*- mode: python ; coding: utf-8 -*-
import os


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[(os.path.expanduser('~/.ytdlp-downloader/bin/yt-dlp'), 'bin'), ('/opt/homebrew/bin/ffmpeg', 'bin'), ('/opt/homebrew/bin/aria2c', 'bin')],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='YT-DLP Downloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='YT-DLP Downloader',
)
app = BUNDLE(
    coll,
    name='YT-DLP Downloader.app',
    icon=None,
    bundle_identifier=None,
)
