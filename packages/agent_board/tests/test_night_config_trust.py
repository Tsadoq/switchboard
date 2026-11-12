import os
import stat
from pathlib import Path

import pytest
from agent_board.night import config as night_config
from agent_board.night.config import VerifierConfigError, trusted_file

ROOT_DIR = stat.S_IFDIR | 0o755
ROOT_FILE = stat.S_IFREG | 0o644
LINK = stat.S_IFLNK | 0o777


class Tree:
    def __init__(self, tmp_path: Path) -> None:
        self.base = tmp_path
        self.entries: dict[Path, tuple[int, int]] = {}

    def add(self, relative: str, mode: int, uid: int = 0) -> Path:
        path = self.base / relative
        self.entries[path] = (mode, uid)
        return path

    def metadata(self, path: Path) -> os.stat_result:
        if path in self.entries:
            mode, uid = self.entries[path]
        elif path in self.base.parents or path == self.base:
            mode, uid = ROOT_DIR, 0
        else:
            raise VerifierConfigError(f"cannot inspect {path}")
        return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tree:
    fake = Tree(tmp_path)
    monkeypatch.setattr(night_config, "_metadata", fake.metadata)
    return fake


def test_a_root_owned_config_in_root_owned_directories_is_trusted(tree: Tree) -> None:
    tree.add("etc", ROOT_DIR)
    trusted_file(tree.add("etc/verifier.toml", ROOT_FILE))


@pytest.mark.parametrize(
    ("file_mode", "file_uid", "directory_mode", "directory_uid"),
    [
        pytest.param(ROOT_FILE, 1000, ROOT_DIR, 0, id="agent-owned-file"),
        pytest.param(stat.S_IFREG | 0o664, 0, ROOT_DIR, 0, id="group-writable-file"),
        pytest.param(LINK, 0, ROOT_DIR, 0, id="symlinked-config"),
        pytest.param(ROOT_FILE, 0, stat.S_IFDIR | 0o777, 0, id="world-writable-directory"),
        pytest.param(ROOT_FILE, 0, ROOT_DIR, 1000, id="agent-owned-directory"),
    ],
)
def test_an_untrusted_config_chain_is_refused_before_reading(
    tree: Tree, file_mode: int, file_uid: int, directory_mode: int, directory_uid: int
) -> None:
    tree.add("etc", directory_mode, directory_uid)
    config = tree.add("etc/verifier.toml", file_mode, file_uid)

    with pytest.raises(VerifierConfigError):
        trusted_file(config)


def test_any_interpreter_symlink_is_refused_even_root_owned_to_a_trusted_target(tree: Tree) -> None:
    tree.add("opt", ROOT_DIR)
    tree.add("opt/bin", ROOT_DIR)
    link = tree.add("opt/bin/python", LINK)
    tree.add("usr", ROOT_DIR)
    tree.add("usr/python3.13", ROOT_FILE | 0o111)

    with pytest.raises(VerifierConfigError, match="regular root-owned file"):
        trusted_file(link)


def test_a_copied_root_owned_interpreter_is_trusted(tree: Tree) -> None:
    tree.add("opt", ROOT_DIR)
    tree.add("opt/bin", ROOT_DIR)

    trusted_file(tree.add("opt/bin/python", ROOT_FILE | 0o111))


def test_a_symlinked_config_is_refused_even_when_root_owned(tree: Tree) -> None:
    tree.add("etc", ROOT_DIR)

    with pytest.raises(VerifierConfigError):
        trusted_file(tree.add("etc/verifier.toml", LINK))


def _config_with_happier(tree: Tree, happier_mode: int) -> Path:
    for directory in ("etc", "opt", "opt/bin", "opt/happier"):
        tree.add(directory, ROOT_DIR)
    python = tree.add("opt/bin/python", ROOT_FILE | 0o111)
    happier = tree.add("opt/happier/happier", happier_mode)
    config = tree.add("etc/verifier.toml", ROOT_FILE)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
box = "huginn"
principal = "verifier@huginn"
namespace = "switchboard"
board_url = "https://board.test.invalid"
signer_key = "/etc/switchboard-night/verifier_ed25519"
state_path = "/var/lib/switchboard-night/journal.sqlite3"
slice = "switchboard-night.slice"

[launcher]
python = "{python}"
module = "agent_board.night.launch"
happier = "{happier}"

[agents."claude@huginn"]
uid = 1001
night_uid = 3001
night_gid = 3001
home = "/var/lib/switchboard-night/happier/claude/home"
happier_home = "/var/lib/switchboard-night/happier/claude"
workdir_root = "/srv/night/claude"

[limits]
memory_max = "600M"
tasks_max = 128
cpu_quota = "100%"
stop_grace_seconds = 5
"""
    )
    return config


def test_the_launcher_happier_binary_is_held_to_the_trusted_file_rule(tree: Tree) -> None:
    config = night_config.load(_config_with_happier(tree, ROOT_FILE | 0o111))

    assert config.launcher_happier == tree.base / "opt/happier/happier"


def test_a_symlinked_happier_binary_is_refused(tree: Tree) -> None:
    with pytest.raises(VerifierConfigError, match="regular root-owned file"):
        night_config.load(_config_with_happier(tree, LINK))
