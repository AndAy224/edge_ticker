"""deploy/update.sh, run in a sandbox: a bare repo as origin, a clone as the app
dir (EDGE_TICKER_DIR), a scratch /etc (EDGE_TICKER_ETC), a stub venv python,
and stand-ins for systemctl/udevadm/npm/npx/curl/journalctl/sleep first on
PATH. Nothing outside tmp_path is touched and nothing leaves the machine."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "update.sh"
UNITS = ("ticker-backend.service", "kiosk.service", "ticker-watchdog.service", "ticker-watchdog.timer")
RULE = "99-edge-ticker-input.rules"
OLD_UNIT = "[Service]\nExecStart=/opt/edge-ticker/.venv/bin/uvicorn backend.main:app\n"
NEW_UNIT = "[Service]\nExecStart=/opt/edge-ticker/.venv/bin/uvicorn backend.main:app --new-flag\n"

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash"
)

STUBS = {
    # Each logs its argv; flag files in the sandbox script failures.
    "systemctl": """
echo "$*" >> "$SANDBOX/systemctl.log"
if [ "$*" = "restart ticker-backend.service" ] && [ -e "$SANDBOX/fail-restart-once" ]; then
  rm "$SANDBOX/fail-restart-once"; exit 1
fi
""",
    "udevadm": 'echo "$*" >> "$SANDBOX/udevadm.log"',
    "npm": "",
    "npx": """
if [ "$1" = tsc ] && [ -e "$SANDBOX/fail-tsc" ]; then exit 2; fi
if [ "$1" = vite ]; then mkdir -p dist.new && echo "new bundle" > dist.new/index.html; fi
""",
    "curl": """
case "$*" in
  *-X\\ POST*) echo "$*" >> "$SANDBOX/curl-post.log"; exit 0 ;;
esac
[ -e "$SANDBOX/healthy" ] || exit 22
echo '{"status": "ok", "display_clients": 1}'
""",
    "journalctl": "",
    "sleep": "",
}


def _git_env() -> dict:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = root / "app"
        self.etc = root / "etc"
        seed = root / "seed"

        script = SCRIPT.read_text()
        write(seed / "deploy" / "update.sh", script, 0o755)
        for unit in UNITS:
            write(seed / "deploy" / unit, OLD_UNIT if unit == "ticker-backend.service" else f"# {unit}\n")
        write(seed / "deploy" / RULE, "# rule v1\n")
        write(seed / "pyproject.toml", '[project]\nname = "x"\ndependencies = []\n')
        write(seed / "frontend" / "package.json", "{}\n")
        git(root, "init", "-q", "-b", "main", str(seed))
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "v1")
        self.v1 = git(seed, "rev-parse", "HEAD")
        git(root, "clone", "-q", "--bare", str(seed), str(root / "origin.git"))
        git(root, "clone", "-q", str(root / "origin.git"), str(self.app))

        # v2 changes the backend unit (a new ExecStart flag) — and the script
        # itself, so its own `git reset` rewrites the file bash is running.
        write(seed / "deploy" / "ticker-backend.service", NEW_UNIT)
        write(seed / "deploy" / "update.sh", script + "# v2\n", 0o755)
        git(seed, "commit", "-q", "-am", "v2")
        self.v2 = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "-q", str(root / "origin.git"), "main")

        # The installed system as v1 left it, plus the live bundle and the venv.
        for unit in UNITS:
            write(self.etc / "systemd" / "system" / unit, (seed / "deploy" / unit).read_text()
                  if unit != "ticker-backend.service" else OLD_UNIT)
        write(self.etc / "udev" / "rules.d" / RULE, "# rule v1\n")
        write(self.app / "frontend" / "dist" / "index.html", "old bundle\n")
        write(self.app / ".venv" / "bin" / "python",
              '#!/bin/sh\n[ "$1" = - ] && cat >/dev/null\nexit 0\n', 0o755)

        self.bin = root / "bin"
        for name, body in STUBS.items():
            write(self.bin / name, f'#!/bin/sh\nSANDBOX="{root}"\n{body}\nexit 0\n', 0o755)

    def flag(self, name: str) -> None:
        (self.root / name).touch()

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = {
            **_git_env(),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "EDGE_TICKER_DIR": str(self.app),
            "EDGE_TICKER_ETC": str(self.etc),
            "EDGE_TICKER_URL": "http://127.0.0.1:9",
        }
        return subprocess.run(["bash", str(self.app / "deploy" / "update.sh"), *args],
                              cwd=self.app, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    @property
    def head(self) -> str:
        return git(self.app, "rev-parse", "HEAD")

    @property
    def installed_unit(self) -> str:
        return (self.etc / "systemd" / "system" / "ticker-backend.service").read_text()

    @property
    def live_bundle(self) -> str:
        return (self.app / "frontend" / "dist" / "index.html").read_text()

    def log(self, name: str) -> list[str]:
        path = self.root / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def assert_restored(sb: Sandbox, result) -> None:
    assert result.returncode == 1, result.stdout + result.stderr
    assert sb.head == sb.v1
    assert sb.installed_unit == OLD_UNIT  # the old code runs under its own unit
    assert sb.live_bundle == "old bundle\n"
    assert not (sb.app / "frontend" / "dist.new").exists()
    assert not (sb.app / ".deploy" / "prev-commit").exists()
    assert "restart ticker-backend.service" in sb.log("systemctl")


def test_healthy_deploy_switches_code_bundle_and_units(sandbox):
    sandbox.flag("healthy")
    result = sandbox.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.head == sandbox.v2
    assert sandbox.installed_unit == NEW_UNIT
    assert sandbox.live_bundle == "new bundle\n"
    assert (sandbox.app / ".deploy" / "dist.prev" / "index.html").read_text() == "old bundle\n"
    assert (sandbox.app / ".deploy" / "prev-commit").read_text().strip() == sandbox.v1
    calls = sandbox.log("systemctl")
    assert calls.count("daemon-reload") == 1 and calls.count("restart ticker-backend.service") == 1
    assert "restart kiosk.service" not in calls
    assert sandbox.log("curl-post")  # the reload for pre-build-id bundles


def test_rollback_flag_returns_to_the_previous_commit_and_its_unit(sandbox):
    sandbox.flag("healthy")
    assert sandbox.run().returncode == 0
    result = sandbox.run("--rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.head == sandbox.v1
    assert sandbox.installed_unit == OLD_UNIT


def test_failed_health_restores_the_old_unit_too(sandbox):
    """The rollback used to bring the old code back under the NEW unit — and if
    the unit change was what broke it, the rollback failed too."""
    result = sandbox.run()
    assert "backend unhealthy after the switch" in result.stdout
    assert_restored(sandbox, result)
    calls = sandbox.log("systemctl")
    assert calls.count("daemon-reload") == 2  # new unit in, old unit back
    assert calls.count("restart ticker-backend.service") == 2


def test_failure_during_the_switch_is_rolled_back(sandbox):
    """A step that fails after the build (here the backend restart under the new
    unit) used to stop the script half-switched with nothing restored."""
    sandbox.flag("healthy")
    sandbox.flag("fail-restart-once")
    result = sandbox.run()
    assert "update failed during the switch" in result.stdout
    assert_restored(sandbox, result)
    assert "previous version is back up" in result.stdout


def test_failure_before_the_switch_leaves_the_live_system_alone(sandbox):
    sandbox.flag("healthy")
    sandbox.flag("fail-tsc")
    result = sandbox.run()
    assert result.returncode != 0
    assert "failed before the switch" in result.stdout
    assert sandbox.head == sandbox.v1
    assert sandbox.installed_unit == OLD_UNIT
    assert sandbox.live_bundle == "old bundle\n"
    assert sandbox.log("systemctl") == []
