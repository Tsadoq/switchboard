import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from agent_board.night.launch import EXIT_ASSIGNMENT, LaunchRefused
from agent_board.night.repositories import MirrorPolicy

PRINCIPAL = "claude@huginn"


class Tree:
    def __init__(self, tmp_path: Path) -> None:
        self.top = tmp_path
        self.policy = tmp_path / "etc" / "repositories.toml"
        self.mirror_root = tmp_path / "mirrors"
        self.mirror = self.mirror_root / "sb-runner.git"
        (self.mirror / "objects").mkdir(parents=True)
        (self.mirror / "HEAD").write_text("ref: refs/heads/main\n")
        self.policy.parent.mkdir()
        self.write()
        for path in (tmp_path / "etc", self.mirror_root, self.mirror, self.mirror / "objects"):
            path.chmod(0o755)
        for path in (self.policy, self.mirror / "HEAD"):
            path.chmod(0o644)

    def write(self, entry: str | None = None) -> None:
        self.policy.write_text(
            entry
            or (
                '[repositories."sb-runner"]\n'
                f'mirror = "{self.mirror}"\n'
                'visibility = "public"\n'
                f'principals = ["{PRINCIPAL}"]\n'
            )
        )

    def resolve(self, key: str = "sb-runner", principal: str = PRINCIPAL) -> Path:
        policy = MirrorPolicy(self.policy, self.mirror_root, owner=os.getuid(), top=self.top)
        return policy.resolve(key, principal)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


def test_a_reviewed_public_mirror_resolves_for_its_principal(tree: Tree) -> None:
    assert tree.resolve() == tree.mirror


def _refused(tree: Tree, reason: str, **kwargs: str) -> None:
    with pytest.raises(LaunchRefused) as refused:
        tree.resolve(**kwargs)
    assert refused.value.exit_code == EXIT_ASSIGNMENT
    assert str(refused.value) == reason


def test_without_a_reviewed_policy_every_key_refuses(tree: Tree) -> None:
    tree.policy.unlink()

    _refused(tree, "repository_policy_not_configured")


def test_the_default_policy_refuses_on_a_host_without_one() -> None:
    if Path("/etc/switchboard-night/repositories.toml").exists():
        pytest.skip("this host has a reviewed policy")
    with pytest.raises(LaunchRefused, match="repository_policy"):
        MirrorPolicy().resolve("sb-runner", PRINCIPAL)


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        pytest.param(
            '[repositories."sb-runner"]\nmirror = "{m}"\nvisibility = "private"\nprincipals = ["claude@huginn"]\n',
            "repository_not_public",
            id="private",
        ),
        pytest.param(
            '[repositories."sb-runner"]\nmirror = "{m}"\nvisibility = "public"\nprincipals = ["pi@huginn"]\n',
            "repository_not_for_principal",
            id="other-principal",
        ),
        pytest.param(
            '[repositories."sb-runner"]\nmirror = "/home/dev/sb-runner"\nvisibility = "public"\n'
            'principals = ["claude@huginn"]\n',
            "repository_mirror_outside_root",
            id="day-checkout",
        ),
        pytest.param(
            '[repositories."sb-runner"]\nmirror = "{m}"\nvisibility = "public"\nprincipals = ["claude@huginn"]\n'
            'url = "https://example.com"\n',
            "repository_not_in_policy",
            id="extra-field",
        ),
        pytest.param("repositories = 1\n", "repository_policy_malformed", id="not-a-table"),
        pytest.param("[repositories]\n[other]\n", "repository_policy_malformed", id="extra-table"),
        pytest.param("not toml = = 1\n", "repository_policy_unreadable", id="not-toml"),
    ],
)
def test_a_policy_outside_the_reviewed_shape_refuses(tree: Tree, entry: str, reason: str) -> None:
    tree.write(entry.replace("{m}", str(tree.mirror)))

    _refused(tree, reason)


def test_unknown_and_malformed_keys_refuse(tree: Tree) -> None:
    _refused(tree, "repository_not_in_policy", key="checkmk")
    _refused(tree, "repository_key_invalid", key="../sb-runner")


def _writable_policy(tree: Tree) -> None:
    tree.policy.chmod(0o664)


def _writable_mirror_file(tree: Tree) -> None:
    (tree.mirror / "HEAD").chmod(0o666)


def _symlink_in_mirror(tree: Tree) -> None:
    (tree.mirror / "objects" / "pack").symlink_to(tree.top)


def _writable_mirror_root(tree: Tree) -> None:
    tree.mirror_root.chmod(0o777)


def _not_bare(tree: Tree) -> None:
    (tree.mirror / "HEAD").unlink()


@pytest.mark.parametrize(
    ("spoil", "reason"),
    [
        pytest.param(_writable_policy, "repository_policy_untrusted", id="writable-policy"),
        pytest.param(_writable_mirror_file, "repository_mirror_untrusted", id="writable-mirror-file"),
        pytest.param(_symlink_in_mirror, "repository_mirror_untrusted", id="symlink-in-mirror"),
        pytest.param(_writable_mirror_root, "repository_mirror_untrusted", id="writable-mirror-root"),
        pytest.param(_not_bare, "repository_mirror_untrusted", id="not-a-bare-repository"),
    ],
)
def test_anything_the_night_uid_could_change_refuses(
    tree: Tree, spoil: Callable[[Tree], None], reason: str
) -> None:
    spoil(tree)

    _refused(tree, reason)


def test_an_acl_on_the_policy_refuses(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.getxattr

    def getxattr(path: Any, name: str, *, follow_symlinks: bool = True) -> bytes:
        if Path(str(path)) == tree.policy and name == "system.posix_acl_access":
            return b"\x02\x00\x00\x00"
        return real(path, name, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "getxattr", getxattr)

    _refused(tree, "repository_policy_untrusted")
