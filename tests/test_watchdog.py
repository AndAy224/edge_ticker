"""deploy/watchdog.sh, run as its unit runs it (/bin/bash), with curl and
systemctl stubbed and its state files in a temp dir."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "watchdog.sh"

KIOSK_UP = {"display_clients": 1, "kiosk_clients": 1}
DRAWING = {"connected_for": 900.0, "ping_age": 4.0, "frame_age": 4.0, "blanked": False}
FROZEN = {"connected_for": 900.0, "ping_age": 4.0, "frame_age": 312.0, "blanked": False}


class Watchdog:
    def __init__(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.health = tmp_path / "health.json"
        self.calls = tmp_path / "systemctl.log"
        stubs = {
            # No file is the backend not answering: curl -f prints nothing.
            "curl": f'#!/bin/sh\ncat "{self.health}" 2>/dev/null\n',
            "systemctl": f'#!/bin/sh\necho "$@" >> "{self.calls}"\n',
        }
        for name, body in stubs.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "WATCHDOG_STATE_DIR": str(self.state),
        }
        self.output = ""
        self.codes: list[int] = []

    def serve(self, body: str | None) -> None:
        """What /health answers from now on; None is no answer at all."""
        if body is None:
            self.health.unlink(missing_ok=True)
        else:
            self.health.write_text(body)

    def check(self, times: int = 1) -> list[str]:
        for _ in range(times):
            done = subprocess.run(
                ["/bin/bash", str(SCRIPT)], env=self.env, capture_output=True, text=True, timeout=30
            )
            self.codes.append(done.returncode)
            self.output += done.stdout + done.stderr
        return self.restarts()

    def restarts(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def __call__(self, report: dict, checks: int) -> list[str]:
        self.serve(json.dumps({"ok": True, "stuck": [], **report}))
        restarts = self.check(checks)
        assert set(self.codes) == {0}, self.output
        return restarts


@pytest.fixture
def watchdog(tmp_path):
    return Watchdog(tmp_path)


def test_a_display_tab_elsewhere_does_not_hide_a_hung_kiosk(watchdog):
    # display_clients counts a laptop's /display tab too; the kiosk is gone.
    assert watchdog({"display_clients": 1, "kiosk_clients": 0}, checks=5) == ["restart kiosk.service"]


def test_a_connected_kiosk_is_left_alone(watchdog):
    assert watchdog({"display_clients": 2, "kiosk_clients": 1}, checks=6) == []


def test_older_backends_fall_back_to_display_clients(watchdog):
    assert watchdog({"display_clients": 1}, checks=6) == []
    assert watchdog({"display_clients": 0}, checks=5) == ["restart kiosk.service"]


def test_a_kiosk_that_comes_back_starts_the_count_again(watchdog):
    assert watchdog({"kiosk_clients": 0}, checks=4) == []
    assert watchdog({"kiosk_clients": 1}, checks=1) == []
    assert watchdog({"kiosk_clients": 0}, checks=4) == []


def test_a_backend_that_stops_answering_is_restarted(watchdog):
    watchdog.serve(None)
    assert watchdog.check(2) == []
    assert watchdog.check(1) == ["restart ticker-backend.service"]


# ---- a connected kiosk whose screen has stopped -----------------------------


def test_a_connected_kiosk_with_a_frozen_screen_is_restarted(watchdog):
    # The compositor's frame loop died under a live page: socket up, pings
    # arriving, every other check passing, one picture on the panel for hours.
    assert watchdog({**KIOSK_UP, "kiosk": FROZEN}, checks=2) == []
    assert watchdog({**KIOSK_UP, "kiosk": FROZEN}, checks=1) == ["restart kiosk.service"]
    assert "frame_age=312s" in watchdog.output


def test_a_kiosk_that_is_drawing_is_left_alone(watchdog):
    assert watchdog({**KIOSK_UP, "kiosk": DRAWING}, checks=8) == []


def test_a_page_whose_pings_stopped_is_frozen_too(watchdog):
    # A hung renderer: Chromium's network stack keeps the socket open on its
    # own. Also all a bundle from before the frame count can tell us.
    silent = {"connected_for": 900.0, "ping_age": 240.0, "frame_age": None, "blanked": False}
    assert watchdog({**KIOSK_UP, "kiosk": silent}, checks=3) == ["restart kiosk.service"]


def test_a_page_that_just_loaded_is_not_judged_yet(watchdog):
    loading = {"connected_for": 45.0, "ping_age": None, "frame_age": None, "blanked": False}
    assert watchdog({**KIOSK_UP, "kiosk": loading}, checks=6) == []
    # ... nor one whose first frames are simply not in yet
    early = {**FROZEN, "connected_for": 100.0, "frame_age": 95.0}
    assert watchdog({**KIOSK_UP, "kiosk": early}, checks=6) == []


def test_a_backend_that_reports_no_frames_is_not_called_frozen(watchdog):
    assert watchdog({**KIOSK_UP}, checks=8) == []
    assert watchdog({**KIOSK_UP, "kiosk": None}, checks=8) == []
    never_pinged = {"connected_for": 900.0, "ping_age": None, "frame_age": None, "blanked": False}
    assert watchdog({**KIOSK_UP, "kiosk": never_pinged}, checks=8) == []


def test_one_slow_minute_is_not_a_frozen_screen(watchdog):
    assert watchdog({**KIOSK_UP, "kiosk": FROZEN}, checks=2) == []
    assert watchdog({**KIOSK_UP, "kiosk": DRAWING}, checks=1) == []
    assert watchdog({**KIOSK_UP, "kiosk": FROZEN}, checks=2) == []


def test_kiosk_restarts_that_do_not_help_stop(watchdog):
    # A panel that is switched off draws no frames either: three tries, then
    # it is left for a person instead of restarting Chromium all night.
    assert watchdog({**KIOSK_UP, "kiosk": FROZEN}, checks=30) == ["restart kiosk.service"] * 3
    assert "after 3 kiosk restarts, leaving it" in watchdog.output
    # Frames seen moving for a while forgive that, and a later freeze is acted on.
    assert watchdog({**KIOSK_UP, "kiosk": DRAWING}, checks=10) == ["restart kiosk.service"] * 3
    assert watchdog({**KIOSK_UP, "kiosk": FROZEN}, checks=3) == ["restart kiosk.service"] * 4


# ---- backend-side trouble a restart fixes -----------------------------------


def test_a_stuck_collector_restarts_the_backend(watchdog):
    assert watchdog({**KIOSK_UP, "stuck": ["adsb"]}, checks=2) == []
    assert watchdog({**KIOSK_UP, "stuck": ["adsb"]}, checks=1) == ["restart ticker-backend.service"]


def test_a_dead_background_task_restarts_the_backend(watchdog):
    # No night schedule, or no Home Assistant, until someone opened the admin.
    report = {**KIOSK_UP, "dead_tasks": ["night-scheduler"]}
    assert watchdog(report, checks=3) == ["restart ticker-backend.service"]
    assert "task:night-scheduler" in watchdog.output


def test_backend_restarts_that_do_not_help_stop(watchdog):
    # A collector that dies at every start: each restart polls every upstream
    # from cold, Launch Library's shared 15 requests an hour included.
    report = {**KIOSK_UP, "stuck": ["launches"]}
    assert watchdog(report, checks=30) == ["restart ticker-backend.service"] * 3
    assert "after 3 backend restarts, leaving it" in watchdog.output
    # A frozen screen is still acted on while that is going on.
    assert watchdog({**report, "kiosk": FROZEN}, checks=3)[-1] == "restart kiosk.service"


# ---- answers and state it cannot read ---------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "<html>502 Bad Gateway</html>",
        "[]",
        json.dumps({"ok": True, "kiosk_clients": None}),
        json.dumps({"ok": True, "kiosk_clients": 1, "stuck": "adsb"}),
    ],
)
def test_an_unreadable_answer_is_an_error_not_all_clear(watchdog, body):
    # This used to read as "kiosk connected, nothing stuck" without a word —
    # and a `stuck` that was a string as collectors named a, d, s and b.
    watchdog.serve(body)
    assert watchdog.check(6) == []
    assert set(watchdog.codes) == {1}
    assert "unreadable" in watchdog.output


@pytest.mark.parametrize("junk", ["abc", "", "3 4"])
def test_a_mangled_counter_file_does_not_stop_the_checks(watchdog, junk):
    # `$(( ))` on "abc" under `set -u` failed every run until the next boot.
    (watchdog.state / "ticker-watchdog.nodisplay").write_text(junk)
    assert watchdog({"kiosk_clients": 0}, checks=5) == ["restart kiosk.service"]
