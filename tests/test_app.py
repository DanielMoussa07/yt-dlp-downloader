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
    assert app.SPEED_FLAGS["Normal"] == []


def _frags(name):
    flags = app.SPEED_FLAGS[name]
    if "--concurrent-fragments" not in flags:
        return 1
    return int(flags[flags.index("--concurrent-fragments") + 1])


def test_connection_budget_under_youtube_throttle():
    # Regression: 4 parallel slots * 16 fragments = 64 simultaneous connections
    # tripped YouTube's nsig throttle and stalled every download at ~0 KB/s.
    # Cap total connections (parallel slots * per-process fragments) so the flood
    # can't recur. >5 connections is where the throttle reliably kicks in.
    for name in app.SPEED_FLAGS:
        assert _frags(name) <= 5, f"{name} fragment count too high: {_frags(name)}"
    parallel = int(app.PARALLEL_DEFAULT)
    assert parallel <= 2, f"PARALLEL_DEFAULT too high: {parallel}"
    worst_case = parallel * _frags("Maximum")
    assert worst_case <= 10, f"worst-case connection flood: {worst_case}"


def test_build_cmd_paces_requests():
    # The throttle-to-0 stall is driven by hammering the metadata/nsig API. The
    # command must pace requests and retry fragments to ride out throttling.
    import inspect
    src = inspect.getsource(app.DownloadSlot._build_cmd)
    assert "--sleep-requests" in src, "must pace API requests to avoid 429 throttle"
    assert "--fragment-retries" in src, "must retry throttled fragments"


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


def test_dl_one_kills_child_spawned_after_cancel():
    # Regression: stop_download snapshots _processes, but _dl_one registered its
    # child only AFTER Popen returned. A cancel firing in that window left an
    # unreaped yt-dlp holding a *.part open (the undeletable-file bug). _dl_one must
    # detect the race and kill its own child instead of leaking it.
    import threading

    class FakeProc:
        def __init__(self):
            self.pid = -999999          # bogus pid -> os.getpgid raises -> p.kill()
            self.killed = False
            self.returncode = -1
        @property
        def stdout(self):
            raise AssertionError("must not read stdout of a cancelled child")
        def wait(self, *a, **k): return -1
        def poll(self): return -1
        def kill(self): self.killed = True

    slot = _FakeSlot()
    slot._stop_requested = False
    slot._pl_lock   = threading.Lock()
    slot._pl_active = 0
    slot._active    = {}
    slot._processes = []
    slot._build_cmd = lambda url, playlist_item=None: ["dummy"]

    fake = FakeProc()
    orig_popen = app.subprocess.Popen

    def fake_popen(*a, **k):
        slot._stop_requested = True      # cancel fires DURING spawn (the race window)
        return fake

    app.subprocess.Popen = fake_popen
    try:
        app.DownloadSlot._dl_one(slot, "url", 1, 1, threading.Semaphore(1))
    finally:
        app.subprocess.Popen = orig_popen

    assert fake.killed, "child spawned after cancel was not killed (would leak a .part)"
    assert fake not in slot._processes, "cancelled child must not be registered"
    assert slot._pl_active == 0, "active counter not released"


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
    test_connection_budget_under_youtube_throttle,
    test_build_cmd_paces_requests,
    test_unit_conversions,
    test_cleanup_partials_removes_only_fragments,
    test_cleanup_partials_survives_missing_dir,
    test_process_group_kill_reaps_children,
    test_dl_one_kills_child_spawned_after_cancel,
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
