"""deploy/install.sh's guard against re-running on a live appliance, in a
sandbox: the app dir is EDGE_TICKER_DIR and PATH holds nothing but logging
stand-ins, so no real command can run — whatever the script reaches."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "install.sh"
BASH = shutil.which("bash")
COMMANDS = ("apt-get", "id", "useradd", "usermod", "git", "python3", "npm", "chown", "chmod",
            "mkdir", "cp", "udevadm", "systemctl", "grep", "sed", "update-grub")

pytestmark = pytest.mark.skipif(not BASH or os.geteuid() == 0, reason="needs bash, and not root")


class Box:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = root / "app"
        self.calls = root / "calls.log"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name in COMMANDS:
            body = f'echo "{name} $*" >> "{self.calls}"\n'
            if name == "systemctl":
                body += f'[ "$1" = is-active ] && [ ! -e "{root}/backend-running" ] && exit 3\n'
            self._script(bin_dir / name, body)

    @staticmethod
    def _script(path: Path, body: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}exit 0\n")
        path.chmod(0o755)

    def installed(self, running: bool) -> "Box":
        (self.app / ".git").mkdir(parents=True)
        (self.app / "frontend").mkdir()
        (self.app / ".env").touch()
        self._script(self.app / ".venv" / "bin" / "pip")
        self._script(self.app / ".venv" / "bin" / "python")
        if running:
            (self.root / "backend-running").touch()
        return self

    def run(self, *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        env = {"PATH": str(self.root / "bin"), "EDGE_TICKER_DIR": str(self.app)}
        result = subprocess.run([BASH, str(SCRIPT), *args], cwd=self.root, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        return result, self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def box(tmp_path) -> Box:
    return Box(tmp_path)


def test_a_running_appliance_is_refused_and_pointed_at_update_sh(box):
    """A re-run used to pull, then build straight into the live bundle."""
    result, calls = box.installed(running=True).run()
    assert result.returncode == 1
    assert "deploy/update.sh" in result.stdout and "--force" in result.stdout
    assert calls == ["systemctl is-active --quiet ticker-backend.service"]


def test_a_fresh_box_installs(box):
    _, calls = box.run()
    assert calls[0] == "apt-get update"  # no checkout: systemd was not even asked
    assert any(call.startswith("git clone ") and call.endswith(str(box.app)) for call in calls)


def test_an_installed_box_whose_backend_is_down_can_be_reinstalled(box):
    result, calls = box.installed(running=False).run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"git -C {box.app} pull --ff-only" in calls


def test_force_runs_over_a_live_appliance_and_restarts_the_backend(box):
    """`enable --now` starts a stopped unit but leaves a running backend on the
    old code over the new checkout."""
    result, calls = box.installed(running=True).run("--force")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0] == "apt-get update"
    assert "systemctl restart ticker-backend.service" in calls
    assert "systemctl enable --now ticker-backend.service" not in calls
