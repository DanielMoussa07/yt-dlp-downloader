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
import threading
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


class _FakeVar:
    """Stand-in for a Tk StringVar/BooleanVar: just a .get()-able value."""
    def __init__(self, value):
        self._value = value
    def get(self):
        return self._value


def _cmd_slot(dir_path="/tmp/dl"):
    """A _FakeSlot wired with the .get()-able vars _build_cmd reads."""
    slot = _FakeSlot(dir_path)
    slot.format_var   = _FakeVar("MP4")
    slot.quality_var  = _FakeVar("Best")
    slot.speed_var    = _FakeVar("Normal")
    slot.playlist_var = _FakeVar(True)
    slot.parallel_var = _FakeVar("4")
    return slot


class _FakeApp:
    """Stand-in for the Tk root: after() is a no-op so deferred UI callbacks
    (progress bar / status label updates) never fire during a parse test."""
    def after(self, ms, fn=None, *args):
        return None


def _parser_slot():
    """A _FakeSlot with the exact attributes _parse_progress reads/mutates."""
    slot = _FakeSlot()
    slot._app           = _FakeApp()
    slot._pl_lock       = threading.Lock()
    slot._active        = {}
    slot._speed_samples = []
    slot._last_ui_upd   = 0.0
    slot._last_pct      = 0.0
    slot._total_bytes   = 0.0
    slot._current_title = ""
    return slot


def _dl_slot():
    """A _FakeSlot with the attributes _dl_one reads/mutates (Popen is faked)."""
    slot = _FakeSlot()
    slot._app            = _FakeApp()
    slot._stop_requested = False
    slot._pl_lock        = threading.Lock()
    slot._active         = {}
    slot._pl_active      = 0
    slot._pl_ok          = 0
    slot._pl_fail        = 0
    slot._processes      = []
    slot._build_cmd      = lambda *a, **k: ["x"]
    slot._parse_progress = lambda *a, **k: None
    return slot


def _install_fake_popen(stdout=(), on_construct=None, track=None):
    """Monkeypatch app.subprocess.Popen with a FakePopen and return the original
    (restore it in a finally). FakePopen exposes .stdout/.pid/.poll()/.wait()/
    .returncode. `stdout` may be an iterable or a zero-arg callable returning one.
    `track` (dict with 'lock'/'live'/'max') records peak concurrency for the
    semaphore test; `on_construct(cmd)` records that a child was actually spawned."""
    orig = app.subprocess.Popen

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid        = 4321
            self.returncode = 0
            self.stdout     = stdout() if callable(stdout) else list(stdout)
            if on_construct is not None:
                on_construct(cmd)
            if track is not None:
                with track["lock"]:
                    track["live"] += 1
                    track["max"]  = max(track["max"], track["live"])

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            time.sleep(0.02)
            if track is not None:
                with track["lock"]:
                    track["live"] -= 1
            return self.returncode

    app.subprocess.Popen = FakePopen
    return orig


# Real yt-dlp progress lines used by the parser tests.
_LINE_B    = "[download]   0.1% of   12.34MiB at  512.00KiB/s ETA 00:24"
_LINE_C    = "[download]  45.2% of  98.76MiB at    9.54MiB/s ETA 00:08"
_LINE_D    = "[download] 100% of 12.34MiB in 00:10"   # completion: matches neither regex
_LINE_DEST = ("[download] Destination: "
              "/x/Rick Astley - Never Gonna Give You Up.f137.mp4")
_TITLE     = "Rick Astley - Never Gonna Give You Up"


# ── Deterministic tests (no network) ────────────────────────────────────────────

def test_speed_flags_no_aria2c():
    # Regression: aria2c left 0-byte .part files on YouTube; must be gone.
    for name, flags in app.SPEED_FLAGS.items():
        assert "aria2c" not in " ".join(flags), f"{name} still references aria2c"
    assert app.SPEED_FLAGS["Maximum"] == ["--concurrent-fragments", str(app.MAX_FRAGMENTS)]
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


def test_playlist_child_cmd_uses_no_playlist_and_video_url():
    # Regression (download stall): each parallel child must download a single video
    # URL with --no-playlist, NOT re-enumerate the whole playlist via
    # --playlist-items (which rate-limits YouTube and throttles fragments to ~0 KB/s).
    video_url = "https://www.youtube.com/watch?v=ABC"
    cmd = app.DownloadSlot._build_cmd(_cmd_slot(), video_url, force_no_playlist=True)
    assert "--no-playlist" in cmd, "child command must pass --no-playlist"
    assert "--playlist-items" not in cmd, "child must NOT re-enumerate the playlist"
    assert cmd[-1] == video_url, f"video URL must be the final arg, got {cmd[-1]!r}"


def test_render_pl_gated_on_pl_running_flag():
    # Regression (frozen counter): the renderer must reschedule on an explicit
    # _pl_running flag, not is_downloading() (which is False between video batches).
    import inspect
    src = inspect.getsource(app.DownloadSlot._render_pl)
    assert "_pl_running" in src, "_render_pl must gate on _pl_running"
    assert "is_downloading()" not in src, "_render_pl must NOT call is_downloading()"


def test_run_playlist_parallel_dispatches_by_video_url():
    # Regression (download stall): parallel runner must set _pl_running and build
    # per-video watch URLs for each child instead of re-passing the playlist URL.
    import inspect
    src = inspect.getsource(app.DownloadSlot._run_playlist_parallel)
    assert "_pl_running" in src, "_run_playlist_parallel must set _pl_running"
    assert "watch?v=" in src, "_run_playlist_parallel must dispatch per-video URLs"


# ── Parallel-engine regression tests (the untested area that let stalls ship) ────

def test_parse_progress_single_mode_tracks_pct_size_speed():
    # (a) Single-video mode: feeding two progress lines updates last pct, total
    # bytes, and appends one speed sample per line.
    slot = _parser_slot()
    app.DownloadSlot._parse_progress(slot, _LINE_B, None, None)
    app.DownloadSlot._parse_progress(slot, _LINE_C, None, None)
    assert slot._last_pct == 0.452, slot._last_pct
    assert slot._total_bytes == app._to_bytes("98.76MiB"), slot._total_bytes
    assert len(slot._speed_samples) == 2, slot._speed_samples


def test_parse_progress_parallel_mode_stashes_per_video_stats():
    # (b) Parallel mode (pl_idx=4): a progress line populates _active[idx] with
    # percent + SI-formatted size/speed (no ETA suffix required in this regex).
    slot = _parser_slot()
    app.DownloadSlot._parse_progress(slot, _LINE_C, 4, 10)
    e = slot._active[4]
    assert e["pct"] == 45.2, e
    assert e["size"] == app._to_si("98.76MiB"), e
    assert e["speed"] == app._to_si("9.54MiB") + "/s", e


def test_parse_progress_destination_sets_title_both_modes():
    # (c) Destination line yields the clean title (format-code ".f137" stripped)
    # in both single mode (_current_title) and parallel mode (_active[idx]["title"]).
    single = _parser_slot()
    app.DownloadSlot._parse_progress(single, _LINE_DEST, None, None)
    assert single._current_title == _TITLE, single._current_title

    par = _parser_slot()
    app.DownloadSlot._parse_progress(par, _LINE_DEST, 4, 10)
    assert par._active[4]["title"] == _TITLE, par._active


def test_parse_progress_ignores_completion_and_garbage():
    # (d) The completion line ("... in 00:10") and unrelated garbage match no
    # progress regex and must NOT mutate state, in either mode.
    for pl_idx in (None, 4):
        slot = _parser_slot()
        app.DownloadSlot._parse_progress(slot, _LINE_D, pl_idx, 10)
        app.DownloadSlot._parse_progress(slot, "totally unrelated noise", pl_idx, 10)
        assert slot._last_pct == 0.0, (pl_idx, slot._last_pct)
        assert slot._speed_samples == [], (pl_idx, slot._speed_samples)
        assert slot._active == {}, (pl_idx, slot._active)


def test_dl_one_honors_semaphore_cap():
    # Regression (P0): the parallel engine must never run more children than the
    # semaphore permits. 6 workers, cap of 2 → peak concurrency must stay <= 2.
    track = {"lock": threading.Lock(), "live": 0, "max": 0}
    slot  = _dl_slot()                       # all children share one DownloadSlot
    orig  = _install_fake_popen(track=track)
    try:
        sem = threading.Semaphore(2)
        threads = [
            threading.Thread(
                target=app.DownloadSlot._dl_one,
                args=(slot, f"https://www.youtube.com/watch?v=v{i}", i + 1, 6, sem),
            )
            for i in range(6)
        ]
        for t in threads: t.start()
        for t in threads: t.join()
    finally:
        app.subprocess.Popen = orig
    assert track["max"] <= 2, f"semaphore cap breached: peak {track['max']} > 2"


def test_dl_one_skips_work_when_already_cancelled():
    # (e) Cancel invariant: if _stop_requested is set before _dl_one runs, it must
    # never spawn a child and must leave all counters at zero.
    slot = _dl_slot()
    slot._stop_requested = True
    spawned = []
    orig = _install_fake_popen(on_construct=spawned.append)
    try:
        app.DownloadSlot._dl_one(slot, "https://www.youtube.com/watch?v=ABC",
                                 1, 1, threading.Semaphore(1))
    finally:
        app.subprocess.Popen = orig
    assert spawned == [], "no child should be spawned after cancel"
    assert slot._pl_ok == 0 and slot._pl_fail == 0, (slot._pl_ok, slot._pl_fail)
    assert slot._pl_active == 0, slot._pl_active


def test_dl_one_cancel_mid_stream_does_not_count_result():
    # (f) Cancel invariant: cancelling while the child is streaming output must not
    # count the video as ok or failed, must drain _pl_active, and must pop the idx.
    slot = _dl_slot()

    def _flip_stdout():
        def gen():
            slot._stop_requested = True          # cancel arrives mid-stream
            yield _LINE_C
        return gen()

    orig = _install_fake_popen(stdout=_flip_stdout)
    try:
        app.DownloadSlot._dl_one(slot, "https://www.youtube.com/watch?v=ABC",
                                 7, 10, threading.Semaphore(1))
    finally:
        app.subprocess.Popen = orig
    assert slot._pl_ok == 0 and slot._pl_fail == 0, (slot._pl_ok, slot._pl_fail)
    assert slot._pl_active == 0, slot._pl_active
    assert 7 not in slot._active, slot._active


def test_stop_download_snapshots_processes_under_lock():
    # Regression (orphaned child / undeletable .part): stop_download must read
    # _processes under _pl_lock, because _dl_one appends to it under the same lock.
    # An unguarded read can miss a child that's mid-Popen and leak an orphan.
    import inspect
    src = inspect.getsource(app.DownloadSlot.stop_download)
    lock_pos = src.find("self._pl_lock")
    read_pos = src.find("list(self._processes)")
    assert lock_pos != -1, "stop_download must acquire _pl_lock"
    assert read_pos != -1, "stop_download must read _processes"
    assert lock_pos < read_pos, "_pl_lock must be acquired BEFORE reading _processes"


def test_parse_progress_handles_estimated_size():
    # Regression: yt-dlp emits estimated sizes as "of ~ 50.00MiB"; the "~ " token
    # used to make BOTH progress regexes fail, silently dropping those updates so
    # the console/ETA stalled. Both modes must now parse them.
    s = _parser_slot()
    app.DownloadSlot._parse_progress(
        s, "[download]  10.0% of ~ 50.00MiB at 2.00MiB/s ETA 00:20", None, None)
    assert abs(s._last_pct - 0.10) < 1e-9, s._last_pct
    assert s._total_bytes == app._to_bytes("50.00MiB"), s._total_bytes
    assert len(s._speed_samples) == 1, s._speed_samples

    s2 = _parser_slot()
    app.DownloadSlot._parse_progress(
        s2, "[download]  33.0% of ~ 12.00MiB at 1.00MiB/s ETA 00:09", 2, 10)
    assert s2._active[2]["pct"] == 33.0, s2._active
    assert s2._active[2]["bytes"] == app._to_bytes("12.00MiB"), s2._active


def test_run_playlist_parallel_uses_bounded_worker_pool():
    # Regression (robustness): the runner must drain a work queue with a fixed pool
    # of n workers, not spawn one thread per playlist item (hundreds for big lists).
    import inspect
    src = inspect.getsource(app.DownloadSlot._run_playlist_parallel)
    assert "queue.Queue" in src, "must use a bounded work queue"
    assert "range(n)" in src, "must spawn exactly n workers, not one per video"


def test_connection_budget_stays_below_youtube_threshold():
    # Safety: a previous build ran 4 parallel × 16 fragments = 64 simultaneous
    # connections and got the IP suspended. Worst case is MAX_PARALLEL × the
    # largest concurrent-fragments value; keep it comfortably under that 64.
    frags = [int(flags[flags.index("--concurrent-fragments") + 1])
             for flags in app.SPEED_FLAGS.values()
             if "--concurrent-fragments" in flags]
    peak_frag  = max(frags) if frags else 1
    worst_case = app.MAX_PARALLEL * peak_frag
    assert worst_case <= 16, f"worst-case {worst_case} connections too close to the 64 limit"
    assert app.MAX_FRAGMENTS == peak_frag, "MAX_FRAGMENTS must match the Maximum preset"


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
    test_playlist_child_cmd_uses_no_playlist_and_video_url,
    test_render_pl_gated_on_pl_running_flag,
    test_run_playlist_parallel_dispatches_by_video_url,
    test_parse_progress_single_mode_tracks_pct_size_speed,
    test_parse_progress_parallel_mode_stashes_per_video_stats,
    test_parse_progress_destination_sets_title_both_modes,
    test_parse_progress_ignores_completion_and_garbage,
    test_dl_one_honors_semaphore_cap,
    test_dl_one_skips_work_when_already_cancelled,
    test_dl_one_cancel_mid_stream_does_not_count_result,
    test_stop_download_snapshots_processes_under_lock,
    test_parse_progress_handles_estimated_size,
    test_run_playlist_parallel_uses_bounded_worker_pool,
    test_connection_budget_stays_below_youtube_threshold,
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
