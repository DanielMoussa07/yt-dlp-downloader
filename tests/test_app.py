#!/usr/bin/env python3
"""Verification tests for the bug fixes in app.py.

No pytest dependency — plain asserts with a tiny runner.
Run with the venv python (it has customtkinter):

    ./venv/bin/python3 tests/test_app.py
    ./venv/bin/python3 tests/test_app.py --no-net   # skip slow network tests

Importing app.py is safe: the launch is guarded by `if __name__ == "__main__"`,
so no Tk window is created on import.
"""

import os
import sys
import time
import signal
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402

# A known small, stable playlist (3Blue1Brown — Neural networks).
PLAYLIST_URL = ("https://www.youtube.com/watch?v=aircAruvnKk"
                "&list=PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi")


class _FakeSlot:
    """Minimal stand-in for DownloadSlot. Methods under test only read the
    attributes we set here (e.g. _cleanup_partials uses only _dir_path)."""
    def __init__(self, dir_path=None):
        self._dir_path = dir_path


# ── Deterministic tests (no network) ────────────────────────────────────────────

def test_speed_flags_no_aria2c():
    # Regression: aria2c left 0-byte .part files on YouTube; must be gone.
    for name, flags in app.SPEED_FLAGS.items():
        assert "aria2c" not in " ".join(flags), f"{name} still references aria2c"
    assert app.SPEED_FLAGS["Maximum"] == ["--concurrent-fragments", "16"]
    assert app.SPEED_FLAGS["Normal"] == []


def test_unit_conversions():
    assert app._to_si("1.21GiB") == "1.30 GB"
    assert app._to_si("512.3MiB") == "537.2 MB"
    assert app._to_si("0B") == "0B"            # unparseable → returned as-is
    assert abs(app._spd_to_bytes("9.54MiB/s") - 9.54 * 1024**2) < 1
    assert app._spd_to_bytes("not a speed") == 0.0
    assert app._fmt_eta(34) == "0:34"
    assert app._fmt_eta(3661) == "1:01:01"
    assert app._fmt_eta(-5) == "0:00"


def test_cleanup_partials_removes_only_fragments():
    # Regression: cancelled downloads left undeletable *.part files (Finder -8058).
    with tempfile.TemporaryDirectory() as d:
        partials = [
            "video.mp4.part",
            "video.f399.mp4.part",
            "video.mp4.part.aria2",
            "video.mp4.ytdl",
            "video.part-Frag123",
            "leftover.aria2",
        ]
        keepers = ["finished.mp4", "notes.txt", "movie.mkv"]
        for n in partials + keepers:
            open(os.path.join(d, n), "w").close()

        app.DownloadSlot._cleanup_partials(_FakeSlot(d))

        remaining = set(os.listdir(d))
        for n in partials:
            assert n not in remaining, f"{n} should have been deleted"
        for n in keepers:
            assert n in remaining, f"{n} should have been kept"


def test_cleanup_partials_survives_missing_dir():
    # Must never raise, even if the dir is gone.
    app.DownloadSlot._cleanup_partials(_FakeSlot("/nonexistent/dir/xyz"))
    app.DownloadSlot._cleanup_partials(_FakeSlot(None))


def test_process_group_kill_reaps_children():
    # Regression: cancel only SIGTERM'd and never escalated, so children lingered
    # holding files open. Verify killpg(SIGKILL) on a session leader actually reaps
    # a stubborn child that ignores SIGTERM.
    # Parent ignores SIGTERM, spawns a child that sleeps; both in a new session.
    script = ("import signal,time,subprocess,sys;"
              "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
              "subprocess.Popen(['sleep','60']);"
              "time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    time.sleep(1)
    pgid = os.getpgid(p.pid)

    # SIGTERM is ignored by the parent — prove escalation is needed.
    os.killpg(pgid, signal.SIGTERM)
    try:
        p.wait(timeout=2)
        raised = False
    except subprocess.TimeoutExpired:
        raised = True
    assert raised, "parent ignored SIGTERM as designed; should still be alive"

    # SIGKILL on the group must reap everything.
    os.killpg(pgid, signal.SIGKILL)
    p.wait(timeout=5)
    assert p.poll() is not None, "process group not reaped by SIGKILL"
    # The sleep child must also be dead (no orphan holding files open).
    try:
        os.killpg(pgid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, "child survived group SIGKILL"


def test_get_playlist_ids_uses_generous_timeout():
    # Regression #4: the call was capped at 30s; verify the source now allows 90s.
    import inspect
    src = inspect.getsource(app.DownloadSlot._get_playlist_ids)
    assert "timeout=90" in src, "playlist fetch must use the 90s timeout"
    assert "--socket-timeout" in src, "playlist fetch should set a socket timeout"
    assert "range(2)" in src, "playlist fetch should retry once"


# ── Network tests (the real end-to-end verification) ────────────────────────────

def test_get_playlist_ids_live():
    # Proves the user's "Could not fetch playlist" is fixed: the exact app method
    # returns the playlist's video IDs within its timeout.
    t0 = time.time()
    ids = app.DownloadSlot._get_playlist_ids(_FakeSlot(), PLAYLIST_URL)
    dt = time.time() - t0
    assert ids, f"no playlist IDs returned (took {dt:.0f}s — likely IP throttled)"
    assert len(ids) >= 5, f"expected the full playlist, got {len(ids)} ids"
    print(f"      -> {len(ids)} ids in {dt:.0f}s")


def test_title_fetch_command_live():
    # Proves the title-fetch phase-1 command (title only, fast path) returns data.
    ytdlp = app._find_bin("yt-dlp")
    t0 = time.time()
    r = subprocess.run(
        [ytdlp, "--flat-playlist", "--playlist-items", "1",
         "--print", "%(playlist_title)s\t%(title)s",
         "--no-warnings", "--socket-timeout", "15", PLAYLIST_URL],
        capture_output=True, text=True, env=app._ENV, timeout=90,
    )
    dt = time.time() - t0
    assert r.returncode == 0 and r.stdout.strip(), \
        f"title fetch failed (rc={r.returncode}, {dt:.0f}s): {r.stderr[:200]}"
    parts = r.stdout.strip().splitlines()[0].split("\t")
    assert parts[0].strip(), "no playlist title parsed"
    print(f"      -> '{parts[0].strip()[:40]}' in {dt:.0f}s")


# ── Runner ──────────────────────────────────────────────────────────────────────

DETERMINISTIC = [
    test_speed_flags_no_aria2c,
    test_unit_conversions,
    test_cleanup_partials_removes_only_fragments,
    test_cleanup_partials_survives_missing_dir,
    test_process_group_kill_reaps_children,
    test_get_playlist_ids_uses_generous_timeout,
]
NETWORK = [
    test_get_playlist_ids_live,
    test_title_fetch_command_live,
]


def main():
    run_net = "--no-net" not in sys.argv
    tests = DETERMINISTIC + (NETWORK if run_net else [])
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed "
          f"({'with' if run_net else 'no'} network tests)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
