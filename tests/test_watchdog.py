"""deploy/watchdog.sh, run as its unit runs it (/bin/bash), with curl and
systemctl stubbed and its state files in a temp dir."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "watchdog.sh"


@pytest.fixture
def watchdog(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "state").mkdir()
    health = tmp_path / "health.json"
    calls = tmp_path / "systemctl.log"
    stubs = {
        "curl": f'#!/bin/sh\ncat "{health}"\n',
        "systemctl": f'#!/bin/sh\necho "$@" >> "{calls}"\n',
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "WATCHDOG_STATE_DIR": str(tmp_path / "state"),
    }

    def run(report: dict, checks: int) -> list[str]:
        health.write_text(json.dumps({"ok": True, "stuck": [], **report}))
        for _ in range(checks):
            subprocess.run(["/bin/bash", str(SCRIPT)], env=env, check=True, capture_output=True, timeout=30)
        return calls.read_text().splitlines() if calls.exists() else []

    return run


def test_a_display_tab_elsewhere_does_not_hide_a_hung_kiosk(watchdog):
    # display_clients counts a laptop's /display tab too; the kiosk is gone.
    assert watchdog({"display_clients": 1, "kiosk_clients": 0}, checks=5) == ["restart kiosk.service"]


def test_a_connected_kiosk_is_left_alone(watchdog):
    assert watchdog({"display_clients": 2, "kiosk_clients": 1}, checks=6) == []


def test_older_backends_fall_back_to_display_clients(watchdog):
    assert watchdog({"display_clients": 1}, checks=6) == []
    assert watchdog({"display_clients": 0}, checks=5) == ["restart kiosk.service"]
