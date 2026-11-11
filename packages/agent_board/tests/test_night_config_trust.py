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
        self.links: dict[Path, str] = {}

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
    monkeypatch.setattr(night_config, "_link_target", lambda path: fake.links[path])
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


def test_an_interpreter_may_be_a_root_venv_symlink_to_a_trusted_target(tree: Tree) -> None:
    tree.add("opt", ROOT_DIR)
    tree.add("opt/bin", ROOT_DIR)
    link = tree.add("opt/bin/python", LINK)
    tree.add("usr", ROOT_DIR)
    target = tree.add("usr/python3.13", ROOT_FILE | 0o111)
    tree.links[link] = str(target)

    trusted_file(link, interpreter=True)


@pytest.mark.parametrize(
    ("link_uid", "target_directory_mode"),
    [
        pytest.param(1000, ROOT_DIR, id="agent-owned-link"),
        pytest.param(0, stat.S_IFDIR | 0o777, id="target-in-writable-directory"),
    ],
)
def test_an_interpreter_substitution_through_the_symlink_is_refused(
    tree: Tree, link_uid: int, target_directory_mode: int
) -> None:
    tree.add("opt", ROOT_DIR)
    tree.add("opt/bin", ROOT_DIR)
    link = tree.add("opt/bin/python", LINK, link_uid)
    tree.add("srv", target_directory_mode)
    target = tree.add("srv/python3.13", ROOT_FILE | 0o111)
    tree.links[link] = str(target)

    with pytest.raises(VerifierConfigError):
        trusted_file(link, interpreter=True)


def test_a_symlinked_config_is_refused_even_when_root_owned(tree: Tree) -> None:
    tree.add("etc", ROOT_DIR)

    with pytest.raises(VerifierConfigError):
        trusted_file(tree.add("etc/verifier.toml", LINK))


def test_an_agent_owned_hop_in_the_middle_of_a_symlink_chain_is_refused(tree: Tree) -> None:
    tree.add("opt", ROOT_DIR)
    first = tree.add("opt/python", LINK)
    tree.add("agent", ROOT_DIR, 1000)
    shim = tree.add("agent/shim", LINK, 1000)
    tree.add("usr", ROOT_DIR)
    target = tree.add("usr/python3.13", ROOT_FILE | 0o111)
    tree.links[first] = str(shim)
    tree.links[shim] = str(target)

    with pytest.raises(VerifierConfigError, match="agent"):
        trusted_file(first, interpreter=True)


def test_a_relative_chain_of_root_links_is_followed_hop_by_hop(tree: Tree) -> None:
    tree.add("opt", ROOT_DIR)
    tree.add("opt/bin", ROOT_DIR)
    first = tree.add("opt/bin/python", LINK)
    second = tree.add("opt/bin/python3", LINK)
    target = tree.add("opt/bin/python3.13", ROOT_FILE | 0o111)
    tree.links[first] = "python3"
    tree.links[second] = "./python3.13"

    trusted_file(first, interpreter=True)
    assert target in tree.entries


def test_a_symlink_cycle_is_refused(tree: Tree) -> None:
    tree.add("opt", ROOT_DIR)
    first = tree.add("opt/a", LINK)
    second = tree.add("opt/b", LINK)
    tree.links[first] = str(second)
    tree.links[second] = str(first)

    with pytest.raises(VerifierConfigError, match="hops"):
        trusted_file(first, interpreter=True)
