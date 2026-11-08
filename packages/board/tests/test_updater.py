import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
UPDATER = REPO / "deploy" / "agent-board-update"

FAKE_INSTALL = """#!/bin/sh
set -eu
here="$(git rev-parse HEAD)"
if [ -e migrate ]; then
	python3 -I -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); \
c.execute("INSERT INTO schema_migrations VALUES (?, 0)", ("0099_new.sql",)); c.commit()' "$AGENT_BOARD_DATABASE"
fi
if [ -e fail ]; then
	echo "board did not answer healthz"
	exit 3
fi
echo "installing $here"
printf '{"commit": "%s", "subject": "s", "date": "d"}' "$here" >"$AGENT_BOARD_INSTALLED"
"""

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >>"$SYSTEMCTL_LOG"
"""


def _load_updater() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("agent_board_update", str(UPDATER))
    spec = importlib.util.spec_from_loader("agent_board_update", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


updater = _load_updater()


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@dataclass
class Host:
    upstream: Path
    paths: Any
    systemctl_log: Path

    def commit(self, subject: str, **files: str) -> str:
        for name, content in files.items():
            (self.upstream / name).write_text(content)
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-q", "--allow-empty", "-m", subject)
        return _git(self.upstream, "rev-parse", "HEAD")

    def remove(self, *names: str) -> None:
        for name in names:
            (self.upstream / name).unlink()

    def install(self, commit: str) -> None:
        self.paths.installed.write_text(json.dumps({"commit": commit, "subject": "s", "date": "d"}))

    def installed(self) -> str:
        commit: str = json.loads(self.paths.installed.read_text())["commit"]
        return commit

    def request(self, name: str, content: str = "") -> None:
        self.paths.request_dir.mkdir(exist_ok=True)
        (self.paths.request_dir / name).write_text(content + "\n")

    def run(self) -> int:
        code: int = updater.main(["agent-board-update"])
        return code

    def status(self) -> dict[str, object]:
        status: dict[str, object] = json.loads(self.paths.status.read_text())
        return status

    def migrations(self) -> set[str]:
        with closing(sqlite3.connect(self.paths.database)) as conn:
            return {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "deploy").mkdir()
    script = upstream / "deploy" / "install-native.sh"
    script.write_text(FAKE_INSTALL)
    script.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    database = tmp_path / "state" / "board.sqlite3"
    database.parent.mkdir()
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, applied_at REAL)")
        conn.execute("INSERT INTO schema_migrations VALUES ('0001_init.sql', 0)")
        conn.commit()
    config = tmp_path / "update.conf"
    config.write_text(f"# test\nREMOTE={upstream}\nBRANCH=main\n")
    env = {
        "AGENT_BOARD_UPDATE_CONFIG": str(config),
        "AGENT_BOARD_UPDATE_DIR": str(tmp_path / "update"),
        "AGENT_BOARD_UPDATE_REQUESTS": str(tmp_path / "state" / "update"),
        "AGENT_BOARD_INSTALLED": str(tmp_path / "installed.json"),
        "AGENT_BOARD_DATABASE": str(database),
        "AGENT_BOARD_RUN_AS": "",
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Host(upstream, updater.Paths.from_env(), tmp_path / "systemctl.log")


def test_check_lists_commits_since_the_installed_one(host: Host) -> None:
    first = host.commit("First release")
    host.install(first)
    host.commit("Second")
    third = host.commit("Third")

    assert host.run() == 0, host.status()

    status = host.status()
    pending = status["pending"]
    assert isinstance(pending, list)
    assert (status["behind"], [c["subject"] for c in pending]) == (2, ["Third", "Second"]), status
    latest = status["latest"]
    assert isinstance(latest, dict)
    assert (latest["commit"], latest["short"], latest["subject"]) == (third, third[:12], "Third"), latest
    assert (status["state"], status["error"]) == ("idle", None), status
    assert host.paths.status.stat().st_mode & 0o777 == 0o644, "status unreadable by the board"


def test_apply_installs_the_requested_commit(host: Host) -> None:
    first = host.commit("First release")
    host.install(first)
    second = host.commit("Second")
    host.request("apply", second)
    host.request("check")

    assert host.run() == 0, host.status()

    status = host.status()
    last = status["last_update"]
    assert isinstance(last, dict)
    assert host.installed() == second, "requested commit not installed"
    assert (last["ok"], last["from"], last["to"]) == (True, first, second), last
    assert status["behind"] == 0, status
    assert list(host.paths.request_dir.iterdir()) == [], "requests left behind"
    assert len(list(host.paths.backups.glob("board-*.sqlite3"))) == 1, "database not backed up"
    log_tail = status["log_tail"]
    assert isinstance(log_tail, list) and f"installing {second}" in log_tail, log_tail


def test_apply_refuses_a_commit_not_on_the_branch(host: Host) -> None:
    first = host.commit("First release")
    host.install(first)
    _git(host.upstream, "switch", "-q", "-c", "side")
    stray = host.commit("Not on main")
    _git(host.upstream, "switch", "-q", "main")
    host.request("apply", stray)

    assert host.run() == 1

    status = host.status()
    assert "is not on main" in str(status["error"]), status
    assert host.installed() == first, "a commit off the branch was installed"
    assert status["behind"] == 0, "the check did not run after the refusal"


def test_apply_refuses_a_request_that_is_not_a_commit_id(host: Host) -> None:
    host.install(host.commit("First release"))
    host.request("apply", "main; rm -rf /")

    assert host.run() == 1

    assert "full commit id" in str(host.status()["error"]), host.status()


def test_failed_update_rolls_back(host: Host) -> None:
    first = host.commit("First release")
    host.install(first)
    broken = host.commit("Broken", fail="")
    host.request("apply", broken)

    assert host.run() == 1

    last = host.status()["last_update"]
    assert isinstance(last, dict)
    assert host.installed() == first, "the previous version was not reinstalled"
    assert (last["ok"], last["rolled_back"]) == (False, True), last
    assert not host.systemctl_log.exists(), "the board was stopped although the schema did not change"


def test_failed_update_that_migrated_restores_the_database(host: Host) -> None:
    first = host.commit("First release")
    host.install(first)
    broken = host.commit("Broken migration", fail="", migrate="")
    host.request("apply", broken)

    assert host.run() == 1

    last = host.status()["last_update"]
    assert isinstance(last, dict)
    assert host.migrations() == {"0001_init.sql"}, "the failed version's migration was kept"
    assert (host.installed(), last["rolled_back"]) == (first, True), last
    assert host.systemctl_log.read_text() == "stop agent-board.service\n", "the board was not stopped first"


def test_failed_update_without_a_known_install_is_not_rolled_back(host: Host) -> None:
    broken = host.commit("Broken", fail="")
    host.request("apply", broken)

    assert host.run() == 1

    last = host.status()["last_update"]
    assert isinstance(last, dict)
    assert (last["ok"], last["rolled_back"]) == (False, False), last


def test_backups_are_pruned(host: Host) -> None:
    host.install(host.commit("First release"))
    releases = [host.commit(f"Release {number}") for number in range(updater.BACKUPS_KEPT + 2)]
    for release in releases:
        host.request("apply", release)
        assert host.run() == 0, host.status()

    kept = sorted(
        backup.name.rpartition("-")[2].removesuffix(".sqlite3") for backup in host.paths.backups.iterdir()
    )
    assert kept == sorted(release[:12] for release in releases[-updater.BACKUPS_KEPT :]), kept


def test_a_symlinked_request_is_dropped_without_reading_its_target(host: Host, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("a" * 40)
    host.paths.request_dir.mkdir()
    (host.paths.request_dir / "apply").symlink_to(secret)

    assert updater.take_request(host.paths.request_dir, "apply") == ""

    assert not (host.paths.request_dir / "apply").is_symlink(), "symlinked request kept"
    assert secret.read_text() == "a" * 40, "the symlink target was touched"


def test_bad_config_is_reported_in_the_status(host: Host) -> None:
    host.paths.config.write_text("BRANCH=..bad\n")

    assert host.run() == 1

    assert "not a branch name" in str(host.status()["error"]), host.status()
