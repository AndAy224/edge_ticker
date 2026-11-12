"""deploy/update.sh, run in a sandbox: a bare repo as origin, a clone as the app
dir (EDGE_TICKER_DIR), a scratch /etc (EDGE_TICKER_ETC), a stub venv python
(with a pip that keeps its "installed packages" in a file), and stand-ins for
systemctl/udevadm/npm/npx/curl/journalctl/sleep first on PATH. Nothing outside
tmp_path is touched and nothing leaves the machine."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
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

# A stub that has to block until the test lets it go (or signals it).
HANG = """
  touch "$SANDBOX/{0}-running"
  while [ ! -e "$SANDBOX/release" ]; do /bin/sleep 0.02; done
"""

STUBS = {
    # Each logs its argv; flag files in the sandbox script failures.
    "systemctl": """
echo "$*" >> "$SANDBOX/systemctl.log"
if [ "$*" = "restart ticker-backend.service" ] && [ -e "$SANDBOX/fail-restart-once" ]; then
  rm "$SANDBOX/fail-restart-once"; exit 1
fi
if [ "$*" = "restart ticker-backend.service" ] && [ -e "$SANDBOX/hang-restart" ]; then
  rm "$SANDBOX/hang-restart"
""" + HANG.format("restart") + """
fi
""",
    "udevadm": 'echo "$*" >> "$SANDBOX/udevadm.log"',
    "npm": """
echo "$*" >> "$SANDBOX/npm.log"
[ -e "$SANDBOX/no-network" ] && exit 1
if [ -e "$SANDBOX/hang-npm" ]; then
  rm "$SANDBOX/hang-npm"
""" + HANG.format("npm") + """
fi
""",
    "npx": """
if [ "$1" = tsc ] && [ -e "$SANDBOX/fail-tsc" ]; then exit 2; fi
if [ "$1" = vite ]; then
  mkdir -p dist.new && echo "bundle of $(git rev-parse HEAD)" > dist.new/index.html
fi
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

# The venv's python: `-` is the dependency install (pip's own rule: a spec that
# is already satisfied is left alone, so `>=` never downgrades), `-c` the import
# check, and `-m pip freeze|install -r|uninstall` work on the same package file.
PYTHON_STUB = """
PKGS="$SANDBOX/venv-packages"
echo "$*" >> "$SANDBOX/python.log"
drop() { grep -v "^$1==" "$PKGS" > "$PKGS.tmp"; mv "$PKGS.tmp" "$PKGS"; }
put() { drop "$1"; echo "$1==$2" | sort -o "$PKGS" - "$PKGS"; }
case "$1 $2 $3" in
  "- "*)
    cat >/dev/null
    [ -e "$SANDBOX/fail-pip" ] && exit 1
    for spec in $(grep '^dependencies' pyproject.toml | grep -o '[a-z]*>=[0-9]*'); do
      name=${spec%%>=*} minimum=${spec##*>=}
      have=$(sed -n "s/^$name==//p" "$PKGS")
      [ "${have:-0}" -ge "$minimum" ] || put "$name" "$minimum"
    done ;;
  "-c "*)
    [ -e "$SANDBOX/fail-import" ] && exit 1 ;;
  "-m pip freeze")
    cat "$PKGS" ;;
  "-m pip install")
    for listing; do :; done  # -r <listing> comes last
    # Offline, a version that is not installed cannot be fetched.
    if [ -e "$SANDBOX/pip-offline" ] && grep -vxFf "$PKGS" "$listing" | grep -q .; then exit 1; fi
    for pin in $(cat "$listing"); do put "${pin%%==*}" "${pin##*==}"; done ;;
  "-m pip uninstall")
    shift 3
    for name; do drop "$name"; done ;;
  *)
    echo "stub python: unexpected $*" >&2; exit 64 ;;
esac
"""


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
    def __init__(self, root: Path, *, new_deps: bool = False, new_kiosk: bool = False) -> None:
        self.root = root
        self.app = root / "app"
        self.etc = root / "etc"
        self.state = self.app / ".deploy"
        seed = root / "seed"

        script = SCRIPT.read_text()
        write(seed / "deploy" / "update.sh", script, 0o755)
        for unit in UNITS:
            write(seed / "deploy" / unit, OLD_UNIT if unit == "ticker-backend.service" else f"# {unit}\n")
        write(seed / "deploy" / RULE, "# rule v1\n")
        write(seed / "pyproject.toml", '[project]\nname = "x"\ndependencies = ["lib>=1"]\n')
        write(seed / "frontend" / "package.json", "{}\n")
        git(root, "init", "-q", "-b", "main", str(seed))
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "v1")
        self.v1 = git(seed, "rev-parse", "HEAD")
        git(root, "clone", "-q", "--bare", str(seed), str(root / "origin.git"))
        git(root, "clone", "-q", str(root / "origin.git"), str(self.app))

        # v2 changes the backend unit (a new ExecStart flag) — and the script
        # itself, so its own `git reset` rewrites the file bash is running.
        # On request also: a bumped library plus a new one, and a changed
        # kiosk unit and udev rule.
        write(seed / "deploy" / "ticker-backend.service", NEW_UNIT)
        write(seed / "deploy" / "update.sh", script + "# v2\n", 0o755)
        if new_deps:
            write(seed / "pyproject.toml", '[project]\nname = "x"\ndependencies = ["lib>=2", "extra>=1"]\n')
        if new_kiosk:
            write(seed / "deploy" / "kiosk.service", "# kiosk.service v2\n")
            write(seed / "deploy" / RULE, "# rule v2\n")
        git(seed, "commit", "-q", "-am", "v2")
        self.v2 = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "-q", str(root / "origin.git"), "main")

        # The installed system as v1 left it, plus the live bundle and the venv.
        for unit in UNITS:
            write(self.etc / "systemd" / "system" / unit,
                  OLD_UNIT if unit == "ticker-backend.service" else f"# {unit}\n")
        write(self.etc / "udev" / "rules.d" / RULE, "# rule v1\n")
        write(self.app / "frontend" / "dist" / "index.html", "old bundle\n")
        write(root / "venv-packages", "lib==1\n")
        self.bin = root / "bin"
        stubs = {self.bin / name: body for name, body in STUBS.items()}
        stubs[self.app / ".venv" / "bin" / "python"] = PYTHON_STUB
        for path, body in stubs.items():
            write(path, f'#!/bin/sh\nSANDBOX="{root}"\n{body}\nexit 0\n', 0o755)

    def flag(self, name: str) -> None:
        (self.root / name).touch()

    def unflag(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)

    def _command(self, args) -> tuple[list[str], dict]:
        env = {
            **_git_env(),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "EDGE_TICKER_DIR": str(self.app),
            "EDGE_TICKER_ETC": str(self.etc),
            "EDGE_TICKER_URL": "http://127.0.0.1:9",
        }
        return ["bash", str(self.app / "deploy" / "update.sh"), *args], env

    def run(self, *args: str) -> subprocess.CompletedProcess:
        command, env = self._command(args)
        return subprocess.run(command, cwd=self.app, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def run_until(self, stub: str, *args: str) -> subprocess.Popen:
        """Start a run and return once the named stub (npm, restart) is hanging.
        The run has its own process group, like a job on a terminal."""
        self.flag(f"hang-{stub}")
        command, env = self._command(args)
        proc = subprocess.Popen(command, cwd=self.app, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        deadline = time.monotonic() + 20
        while not (self.root / f"{stub}-running").exists():
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "the stub never ran"
            time.sleep(0.01)
        return proc

    def finish(self, proc: subprocess.Popen) -> subprocess.CompletedProcess:
        self.flag("release")
        out, _ = proc.communicate(timeout=60)
        for name in ("release", "npm-running", "restart-running"):
            self.unflag(name)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, "")

    @property
    def head(self) -> str:
        return git(self.app, "rev-parse", "HEAD")

    def installed(self, name: str = "ticker-backend.service") -> str:
        folder = self.etc / "udev" / "rules.d" if name == RULE else self.etc / "systemd" / "system"
        return (folder / name).read_text()

    @property
    def installed_unit(self) -> str:
        return self.installed()

    @property
    def live_bundle(self) -> str:
        return (self.app / "frontend" / "dist" / "index.html").read_text()

    def bundle_of(self, commit: str) -> str:
        return f"bundle of {commit}\n"

    @property
    def packages(self) -> str:
        return (self.root / "venv-packages").read_text()

    def record(self, name: str) -> str | None:
        path = self.state / name
        return path.read_text().strip() if path.exists() else None

    def log(self, name: str) -> list[str]:
        path = self.root / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def deployed(sb: Sandbox) -> Sandbox:
    """The sandbox after one healthy deploy of v2."""
    sb.flag("healthy")
    result = sb.run()
    assert result.returncode == 0, result.stdout + result.stderr
    return sb


def assert_restored(sb: Sandbox, result) -> None:
    assert result.returncode == 1, result.stdout + result.stderr
    assert sb.head == sb.v1
    assert sb.installed_unit == OLD_UNIT  # the old code runs under its own unit
    assert sb.live_bundle == "old bundle\n"
    assert sb.packages == "lib==1\n"
    assert not (sb.app / "frontend" / "dist.new").exists()
    for name in ("prev-commit", "deployed", "unfinished"):
        assert sb.record(name) is None, name
    assert "restart ticker-backend.service" in sb.log("systemctl")


def test_healthy_deploy_switches_code_bundle_and_units(sandbox):
    sandbox.flag("healthy")
    result = sandbox.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.head == sandbox.v2
    assert sandbox.installed_unit == NEW_UNIT
    assert sandbox.live_bundle == sandbox.bundle_of(sandbox.v2)
    assert (sandbox.state / "dist.prev" / "index.html").read_text() == "old bundle\n"
    assert sandbox.record("prev-commit") == sandbox.v1
    assert sandbox.record("deployed") == sandbox.v2
    assert sandbox.record("unfinished") is None
    calls = sandbox.log("systemctl")
    assert calls.count("daemon-reload") == 1 and calls.count("restart ticker-backend.service") == 1
    assert "restart kiosk.service" not in calls
    assert sandbox.log("udevadm") == []
    assert sandbox.log("curl-post")  # the reload for pre-build-id bundles


def test_a_changed_kiosk_unit_and_udev_rule_are_applied(tmp_path):
    sb = deployed(Sandbox(tmp_path, new_kiosk=True))
    assert sb.installed("kiosk.service") == "# kiosk.service v2\n"
    assert sb.installed(RULE) == "# rule v2\n"
    assert "restart kiosk.service" in sb.log("systemctl")
    assert sb.log("udevadm") == ["control --reload-rules", "trigger --subsystem-match=input"]
    assert not sb.log("curl-post")  # the restarted kiosk loads the new bundle anyway


def test_a_failed_deploy_puts_the_kiosk_unit_and_udev_rule_back(tmp_path):
    sb = Sandbox(tmp_path, new_kiosk=True)
    assert_restored(sb, sb.run())
    assert sb.installed("kiosk.service") == "# kiosk.service\n"
    assert sb.installed(RULE) == "# rule v1\n"
    assert len(sb.log("udevadm")) == 4  # reloaded for the new rule, and for the old one again


# ---- already deployed, or only checked out ------------------------------------------


def test_a_second_run_finds_the_deploy_done(sandbox):
    deployed(sandbox)
    result = sandbox.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already at" in result.stdout
    assert len(sandbox.log("npm")) == 1  # nothing was rebuilt
    assert sandbox.log("systemctl").count("restart ticker-backend.service") == 1
    assert sandbox.record("prev-commit") == sandbox.v1


def test_head_at_the_target_is_not_a_finished_deploy(sandbox):
    """A run that died in the build left HEAD at the target with the old bundle
    and backend live; the next run printed "already at" and did nothing."""
    git(sandbox.app, "fetch", "-q", "origin", "main")
    git(sandbox.app, "reset", "-q", "--hard", sandbox.v2)
    sandbox.flag("healthy")
    result = sandbox.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already at" not in result.stdout
    assert sandbox.live_bundle == sandbox.bundle_of(sandbox.v2)
    assert sandbox.installed_unit == NEW_UNIT
    assert sandbox.log("systemctl").count("restart ticker-backend.service") == 1
    assert sandbox.record("deployed") == sandbox.v2
    # Nothing is known about what ran before: --rollback must not point here.
    assert sandbox.record("prev-commit") is None

    result = sandbox.run()  # and only once
    assert "already at" in result.stdout
    assert len(sandbox.log("npm")) == 1
    assert sandbox.log("systemctl").count("restart ticker-backend.service") == 1


def test_finishing_a_checked_out_commit_that_fails_its_checks_changes_nothing_live(sandbox):
    git(sandbox.app, "fetch", "-q", "origin", "main")
    git(sandbox.app, "reset", "-q", "--hard", sandbox.v2)
    sandbox.flag("healthy")
    sandbox.flag("fail-tsc")
    result = sandbox.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert sandbox.live_bundle == "old bundle\n"
    assert sandbox.log("systemctl") == []
    assert sandbox.record("deployed") is None  # still unfinished: the next run tries again
    sandbox.unflag("fail-tsc")
    assert sandbox.run().returncode == 0
    assert sandbox.record("deployed") == sandbox.v2


def test_already_deployed_restarts_the_backend_for_a_changed_unit(sandbox):
    """The unit was copied and daemon-reloaded, and then waited for whatever
    restarted the backend next."""
    deployed(sandbox)
    drifted = OLD_UNIT + "# edited by hand\n"
    write(sandbox.etc / "systemd" / "system" / "ticker-backend.service", drifted)
    result = sandbox.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already at" in result.stdout
    assert sandbox.installed_unit == NEW_UNIT
    assert sandbox.log("systemctl")[-3:] == [
        "daemon-reload", "restart ticker-watchdog.timer", "restart ticker-backend.service"]


def test_already_deployed_puts_the_unit_back_when_the_backend_stays_down(sandbox):
    deployed(sandbox)
    drifted = OLD_UNIT + "# edited by hand\n"
    write(sandbox.etc / "systemd" / "system" / "ticker-backend.service", drifted)
    sandbox.unflag("healthy")
    result = sandbox.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "unhealthy under the changed unit" in result.stdout
    assert sandbox.installed_unit == drifted
    assert sandbox.log("systemctl")[-2:] == ["daemon-reload", "restart ticker-backend.service"]
    assert sandbox.head == sandbox.v2 and sandbox.record("deployed") == sandbox.v2


# ---- interrupted and killed runs ------------------------------------------------------


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGINT, signal.SIGTERM])
def test_an_interrupted_build_restores_the_checkout(sandbox, sig):
    """Ctrl-C or a dropped SSH session during `npm ci` used to leave HEAD at the
    target with nothing restored and nothing recorded."""
    sandbox.flag("healthy")
    proc = sandbox.run_until("npm")
    assert sandbox.head == sandbox.v2
    os.killpg(proc.pid, sig)  # the terminal signals the whole job
    result = sandbox.finish(proc)
    assert result.returncode == 1, result.stdout
    assert "before the switch — restoring" in result.stdout
    assert sandbox.head == sandbox.v1
    assert sandbox.live_bundle == "old bundle\n" and sandbox.installed_unit == OLD_UNIT
    assert not (sandbox.app / "frontend" / "dist.new").exists()
    assert sandbox.record("unfinished") is None and sandbox.record("deployed") is None
    assert sandbox.log("systemctl") == []

    result = sandbox.run()  # and the next run is an ordinary deploy
    assert result.returncode == 0, result.stdout + result.stderr
    assert "updating" in result.stdout
    assert sandbox.record("prev-commit") == sandbox.v1


def test_a_signal_to_the_script_alone_takes_effect_when_the_step_returns(sandbox):
    sandbox.flag("healthy")
    proc = sandbox.run_until("npm")
    os.kill(proc.pid, signal.SIGTERM)
    result = sandbox.finish(proc)
    assert result.returncode == 1, result.stdout
    assert "interrupted before the switch" in result.stdout
    assert sandbox.head == sandbox.v1
    assert sandbox.log("systemctl") == []


def test_a_hangup_restores_even_with_nowhere_left_to_write(sandbox):
    """`update.sh | tee log` over SSH: the session goes, tee with it, and the
    restore's first message would kill the script with SIGPIPE."""
    sandbox.flag("healthy")
    proc = sandbox.run_until("npm")
    proc.stdout.close()
    os.killpg(proc.pid, signal.SIGHUP)
    assert proc.wait(timeout=60) == 1
    assert sandbox.head == sandbox.v1
    assert not (sandbox.app / "frontend" / "dist.new").exists()
    assert sandbox.record("unfinished") is None


def test_an_interruption_during_the_switch_rolls_back(sandbox):
    sandbox.flag("healthy")
    proc = sandbox.run_until("restart")
    assert sandbox.live_bundle == sandbox.bundle_of(sandbox.v2)  # mid-switch
    os.kill(proc.pid, signal.SIGHUP)
    result = sandbox.finish(proc)
    assert "interrupted during the switch" in result.stdout
    assert_restored(sandbox, result)
    assert "previous version is back up" in result.stdout


def test_a_killed_run_is_finished_by_the_next_one(sandbox):
    """kill -9 or a power cut restores nothing — but the run left a record of
    what was live, so the next one knows the checkout is ahead of it."""
    sandbox.flag("healthy")
    proc = sandbox.run_until("npm")
    os.killpg(proc.pid, signal.SIGKILL)
    sandbox.finish(proc)
    assert sandbox.head == sandbox.v2 and sandbox.live_bundle == "old bundle\n"

    result = sandbox.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "never finished" in result.stdout
    assert sandbox.live_bundle == sandbox.bundle_of(sandbox.v2)
    assert sandbox.record("deployed") == sandbox.v2
    assert sandbox.record("prev-commit") == sandbox.v1  # --rollback knows where it came from
    assert sandbox.record("unfinished") is None


def test_a_killed_run_that_cannot_be_finished_goes_back_to_what_was_live(tmp_path):
    sb = Sandbox(tmp_path, new_deps=True)
    proc = sb.run_until("npm")
    os.killpg(proc.pid, signal.SIGKILL)
    sb.finish(proc)
    assert sb.head == sb.v2 and sb.packages == "extra==1\nlib==2\n"

    result = sb.run()  # the backend does not come up healthy
    assert "backend unhealthy after the switch" in result.stdout
    assert_restored(sb, result)  # v1, and the packages the killed run found


def test_a_run_killed_inside_the_switch_still_goes_back_to_the_old_bundle(sandbox):
    """What is live by then is the killed run's own bundle; the one it moved
    aside is the previous one, and must not be replaced by it."""
    sandbox.flag("healthy")
    proc = sandbox.run_until("restart")
    os.killpg(proc.pid, signal.SIGKILL)
    sandbox.finish(proc)
    assert sandbox.live_bundle == sandbox.bundle_of(sandbox.v2) and sandbox.installed_unit == NEW_UNIT

    sandbox.unflag("healthy")
    result = sandbox.run()
    assert "backend unhealthy after the switch" in result.stdout
    assert_restored(sandbox, result)


def test_a_record_the_checkout_has_moved_away_from_is_ignored(sandbox):
    git(sandbox.app, "fetch", "-q", "origin", "main")
    write(sandbox.state / "unfinished", f"{sandbox.v2}\n{sandbox.v2}\n")
    result = sandbox.run()  # unhealthy
    assert "never finished" not in result.stdout
    assert_restored(sandbox, result)  # to v1, where the checkout was — not the record's commit


# ---- --rollback ---------------------------------------------------------------------------


def test_rollback_flag_returns_to_the_previous_commit_and_its_unit(sandbox):
    deployed(sandbox)
    result = sandbox.run("--rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.head == sandbox.v1
    assert sandbox.installed_unit == OLD_UNIT
    # The bundle from before the sandbox's first deploy names no commit (as one
    # built by an older update.sh): it can't be trusted to be v1's, so rebuild.
    assert sandbox.live_bundle == sandbox.bundle_of(sandbox.v1)
    assert len(sandbox.log("npm")) == 2
    assert sandbox.record("deployed") == sandbox.v1
    assert sandbox.record("prev-commit") == sandbox.v2


def test_rollback_reuses_the_saved_bundle_without_the_network(sandbox):
    """--rollback reran `npm ci` and the build although the previous bundle was
    still on disk — no rollback while the network was down."""
    deployed(sandbox)
    assert sandbox.run("--rollback").returncode == 0  # live: v1's bundle; saved: v2's
    sandbox.flag("no-network")
    sandbox.flag("fail-tsc")
    for commit in (sandbox.v2, sandbox.v1, sandbox.v2):
        result = sandbox.run("--rollback")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "the bundle saved from" in result.stdout
        assert sandbox.head == commit
        assert sandbox.live_bundle == sandbox.bundle_of(commit)
    assert len(sandbox.log("npm")) == 2  # the deploy and the first rollback only


def test_rollback_rebuilds_when_the_saved_bundle_is_another_commits(sandbox):
    deployed(sandbox)
    assert sandbox.run("--rollback").returncode == 0
    write(sandbox.state / "dist.prev" / ".commit", sandbox.v1 + "\n")  # not v2's
    result = sandbox.run("--rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the bundle saved from" not in result.stdout
    assert len(sandbox.log("npm")) == 3
    assert sandbox.live_bundle == sandbox.bundle_of(sandbox.v2)


def test_a_rollback_that_comes_up_unhealthy_returns_to_the_newer_version(sandbox):
    deployed(sandbox)
    sandbox.unflag("healthy")
    result = sandbox.run("--rollback")
    assert result.returncode == 1, result.stdout + result.stderr
    assert sandbox.head == sandbox.v2 and sandbox.installed_unit == NEW_UNIT
    assert sandbox.live_bundle == sandbox.bundle_of(sandbox.v2)
    assert sandbox.record("deployed") == sandbox.v2 and sandbox.record("prev-commit") == sandbox.v1


def test_rollback_with_nothing_recorded_refuses(sandbox):
    result = sandbox.run("--rollback")
    assert result.returncode == 1
    assert "no previous deploy recorded" in result.stdout
    assert sandbox.head == sandbox.v1 and sandbox.log("systemctl") == []


def test_rollback_to_the_commit_that_is_live_is_refused(sandbox):
    deployed(sandbox)
    write(sandbox.state / "prev-commit", sandbox.v2 + "\n")
    result = sandbox.run("--rollback")
    assert result.returncode == 1
    assert "already at the recorded previous commit" in result.stdout


def test_rollback_restores_the_packages_the_commit_ran_with(tmp_path):
    """`pip install 'lib>=1'` leaves lib 2 in place: the previous commit came
    back on the libraries of the one being rolled back."""
    sb = deployed(Sandbox(tmp_path, new_deps=True))
    assert sb.packages == "extra==1\nlib==2\n"
    assert sb.run("--rollback").returncode == 0
    assert sb.packages == "lib==1\n"
    assert sb.run("--rollback").returncode == 0  # and forward again
    assert sb.packages == "extra==1\nlib==2\n"


def test_rollback_offline_keeps_the_packages_and_says_so(tmp_path):
    sb = deployed(Sandbox(tmp_path, new_deps=True))
    sb.flag("pip-offline")
    result = sb.run("--rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "could not reinstall the packages" in result.stdout
    assert sb.head == sb.v1 and sb.packages == "extra==1\nlib==2\n"


# ---- failures --------------------------------------------------------------------------------


def test_failed_health_restores_the_old_unit_too(sandbox):
    """The rollback used to bring the old code back under the NEW unit — and if
    the unit change was what broke it, the rollback failed too."""
    result = sandbox.run()
    assert "backend unhealthy after the switch" in result.stdout
    assert_restored(sandbox, result)
    calls = sandbox.log("systemctl")
    assert calls.count("daemon-reload") == 2  # new unit in, old unit back
    assert calls.count("restart ticker-backend.service") == 2


def test_failed_health_restores_the_python_packages(tmp_path):
    """The rollback reinstalled the old commit's `>=` list, which downgrades
    nothing: a deploy that was unhealthy because of a bumped library rolled the
    code back onto that same library."""
    sb = Sandbox(tmp_path, new_deps=True)
    result = sb.run()
    assert "backend unhealthy after the switch" in result.stdout
    assert_restored(sb, result)
    assert sb.record("packages.before") is None and sb.record("packages.unrestored") is None


def test_packages_that_cannot_be_restored_offline_are_reported(tmp_path):
    sb = Sandbox(tmp_path, new_deps=True)
    sb.flag("pip-offline")
    result = sb.run()
    assert result.returncode == 1
    assert "PYTHON PACKAGES NOT RESTORED" in result.stdout
    assert sb.head == sb.v1 and sb.live_bundle == "old bundle\n"  # the rest is back
    assert sb.packages == "extra==1\nlib==2\n"
    assert sb.record("packages.unrestored") == "lib==1"  # what to install once online
    assert "restart ticker-backend.service" in sb.log("systemctl")

    sb.flag("healthy")  # a later deploy that works makes the note moot
    assert sb.run().returncode == 0
    assert sb.record("packages.unrestored") is None


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


@pytest.mark.parametrize("step", ["fail-pip", "fail-import", "fail-tsc"])
def test_a_failed_check_puts_the_venv_back_before_anything_restarts(tmp_path, step):
    """The new dependencies go into the live venv before any check; the abort
    left them there for the next backend restart."""
    sb = Sandbox(tmp_path, new_deps=True)
    sb.flag("healthy")
    sb.flag(step)
    result = sb.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "failed before the switch" in result.stdout
    assert sb.head == sb.v1 and sb.packages == "lib==1\n"
    assert sb.live_bundle == "old bundle\n" and sb.log("systemctl") == []
    assert sb.record("unfinished") is None
