import functools
import hashlib
import http.server
import os
import shutil
import stat
import subprocess
import threading
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from .conftest import FakeSign

REPO = Path(__file__).resolve().parents[3]
INSTALLER = REPO / "installer" / "install.sh"
SKILL_SOURCE = REPO / "skill" / "agent-board"
DIST_SKILL_DIR = "skill/agent-board"
CHECKSUMS = "SHA256SUMS"
UPGRADE_PROBE = "agent_board/upgrade_probe.txt"
# The Agent Skills format caps a description at 1024 characters.
DESCRIPTION_LIMIT = 1024
STATE_DIR_MODE = 0o700
_INSTALL_SECONDS = 300
_BUILD_SECONDS = 120

type Snapshot = dict[str, tuple[int, int]]


@dataclass(frozen=True)
class Installer:
    home: Path
    tool_dir: Path
    bin_dir: Path
    shared_uv_dirs: Mapping[str, str]
    workdir: Path

    @property
    def skill_dir(self) -> Path:
        return self.home / ".agents" / "skills" / "agent-board"

    @property
    def board_home(self) -> Path:
        return self.home / ".agents" / "board"

    @property
    def claude_skill_link(self) -> Path:
        return self.home / ".claude" / "skills" / "agent-board"

    @property
    def owned_paths(self) -> list[Path]:
        return [self.skill_dir, self.board_home, self.claude_skill_link, self.bin_dir]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {name: value for name, value in os.environ.items() if name != "VIRTUAL_ENV"}
        env |= {
            **self.shared_uv_dirs,
            "UV_TOOL_DIR": str(self.tool_dir),
            "UV_TOOL_BIN_DIR": str(self.bin_dir),
        }
        return subprocess.run(
            ["bash", str(INSTALLER), *args],
            env=env,
            cwd=self.workdir,
            capture_output=True,
            text=True,
            timeout=_INSTALL_SECONDS,
            check=False,
        )


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def _serving(site: Path) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _snapshot(roots: Iterable[Path]) -> Snapshot:
    entries: Snapshot = {}
    for root in roots:
        if not root.is_symlink() and not root.exists():
            continue
        for path in [root] if root.is_symlink() else [root, *root.rglob("*")]:
            status = path.lstat()
            entries[str(path)] = (status.st_size, status.st_mtime_ns)
    return entries


def _tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _mode(path: Path) -> int | None:
    return stat.S_IMODE(path.stat().st_mode) if path.is_dir() else None


def _write_checksums(dist: Path) -> None:
    served = sorted(path for path in dist.rglob("*") if path.is_file() and path.name != CHECKSUMS)
    (dist / CHECKSUMS).write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(dist).as_posix()}\n"
            for path in served
        )
    )


def _frontmatter(skill_md: Path) -> dict[str, str]:
    _, block, _ = skill_md.read_text().split("---\n", 2)
    return dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)


@pytest.fixture(scope="session")
def dist_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    site = tmp_path_factory.mktemp("site")
    dist = site / "dist"
    subprocess.run(
        ["uv", "build", "--package", "agent-board", "--wheel", "-o", str(dist)],
        cwd=REPO,
        capture_output=True,
        timeout=_BUILD_SECONDS,
        check=True,
    )
    for source in SKILL_SOURCE.rglob("*"):
        if source.is_file():
            target = dist / DIST_SKILL_DIR / source.relative_to(SKILL_SOURCE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    _write_checksums(dist)
    return site


# Session scope runs this before `fake_sign` points HOME at tmp_path, so every install reuses
# the cache and interpreters `uv sync` already fetched.
@pytest.fixture(scope="session")
def shared_uv_dirs() -> dict[str, str]:
    def uv_dir(*args: str) -> str:
        return subprocess.run(
            ["uv", *args], capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()

    return {"UV_CACHE_DIR": uv_dir("cache", "dir"), "UV_PYTHON_INSTALL_DIR": uv_dir("python", "dir")}


@pytest.fixture
def board_url(dist_site: Path) -> Iterator[str]:
    with _serving(dist_site) as url:
        yield url


@pytest.fixture
def installer(fake_sign: "FakeSign", tmp_path: Path, shared_uv_dirs: dict[str, str]) -> Installer:
    return Installer(
        home=Path.home(),
        tool_dir=tmp_path / "uv-tools",
        bin_dir=tmp_path / "uv-bin",
        shared_uv_dirs=shared_uv_dirs,
        workdir=tmp_path,
    )


def test_installer_second_run_changes_nothing_it_owns(
    fake_sign: "FakeSign", installer: Installer, board_url: str
) -> None:
    fake_sign.configure(box="testbox", kinds=["claude", "pi"])
    run1 = installer.run("--board", board_url)
    assert run1.returncode == 0, f"first run failed: {run1.returncode} {run1.stderr}"
    before = _snapshot(installer.owned_paths)
    missing = [str(path) for path in installer.owned_paths if str(path) not in before]
    assert not missing, f"first run did not create {missing}"

    run2 = installer.run("--board", board_url)
    after = _snapshot(installer.owned_paths)

    assert run2.returncode == 0, f"second run failed: {run2.returncode} {run2.stderr}"
    diff = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    assert before == after, f"installer not idempotent: changed {diff}"


def test_installer_wires_cli_skill_symlink_and_both_kind_dirs(
    fake_sign: "FakeSign", installer: Installer, board_url: str
) -> None:
    fake_sign.configure(box="testbox", kinds=["claude", "pi"])

    run = installer.run("--board", board_url)

    assert run.returncode == 0, f"install failed: {run.returncode} {run.stderr}"
    version = subprocess.run(
        [str(installer.bin_dir / "agent-board"), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert version.returncode == 0, f"installed CLI does not run: {version.returncode} {version.stderr}"
    assert _tree(installer.skill_dir) == _tree(SKILL_SOURCE), "installed skill differs from skill/agent-board"
    link = installer.claude_skill_link
    target = link.readlink() if link.is_symlink() else None
    assert target == installer.skill_dir, f"{link} should be a symlink to {installer.skill_dir}, is {target}"
    modes = {kind: _mode(installer.board_home / kind) for kind in ("claude", "pi")}
    assert modes == {"claude": STATE_DIR_MODE, "pi": STATE_DIR_MODE}, f"kind state dirs: {modes}"


def test_installer_refuses_real_directory_in_claude_skills(
    fake_sign: "FakeSign", installer: Installer, board_url: str
) -> None:
    fake_sign.configure(box="testbox", kinds=["claude", "pi"])
    squatter = installer.claude_skill_link
    squatter.mkdir(parents=True)
    (squatter / "SKILL.md").write_text("a hand-made skill\n")
    before = _snapshot([squatter])

    run = installer.run("--board", board_url)

    assert run.returncode == 1, f"expected a refusal: {run.returncode} {run.stderr}"
    assert str(squatter) in run.stderr, f"the refusal does not name {squatter}: {run.stderr}"
    assert _snapshot([squatter]) == before, f"{squatter} was modified"


def test_installer_no_skill_leaves_both_skill_directories_alone(
    fake_sign: "FakeSign", installer: Installer, board_url: str
) -> None:
    fake_sign.configure(box="testbox", kinds=["claude", "pi"])
    squatter = installer.claude_skill_link
    squatter.mkdir(parents=True)
    (squatter / "SKILL.md").write_text("a hand-made skill\n")
    before = _snapshot([squatter])

    run = installer.run("--board", board_url, "--no-skill")

    assert run.returncode == 0, f"--no-skill run failed: {run.returncode} {run.stderr}"
    assert not installer.skill_dir.exists(), f"--no-skill installed {installer.skill_dir}"
    assert _snapshot([squatter]) == before, f"--no-skill modified {squatter}"


def test_installer_rejects_checksum_mismatch(
    fake_sign: "FakeSign", installer: Installer, dist_site: Path, tmp_path: Path
) -> None:
    fake_sign.configure(box="testbox", kinds=["claude", "pi"])
    tampered = tmp_path / "tampered-site"
    shutil.copytree(dist_site, tampered)
    wheel = next((tampered / "dist").glob("*.whl"))
    with wheel.open("ab") as appended:
        appended.write(b"tampered")

    with _serving(tampered) as url:
        run = installer.run("--board", url)

    assert run.returncode == 1, f"expected a checksum refusal: {run.returncode} {run.stderr}"
    assert "mismatch" in run.stderr and wheel.name in run.stderr, (
        f"the refusal does not name the tampered {wheel.name}: {run.stderr}"
    )
    assert not (installer.bin_dir / "agent-board").exists(), "the tampered wheel was installed"


def test_installer_reinstalls_when_the_served_wheel_changes(
    fake_sign: "FakeSign", installer: Installer, dist_site: Path, tmp_path: Path
) -> None:
    fake_sign.configure(box="testbox", kinds=["claude", "pi"])
    with _serving(dist_site) as url:
        first = installer.run("--board", url)
    assert first.returncode == 0, f"first run failed: {first.returncode} {first.stderr}"
    upgraded = tmp_path / "upgraded-site"
    shutil.copytree(dist_site, upgraded)
    wheel = next((upgraded / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel, "a") as contents:
        contents.writestr(UPGRADE_PROBE, "upgraded\n")
    _write_checksums(upgraded / "dist")

    with _serving(upgraded) as url:
        second = installer.run("--board", url)

    assert second.returncode == 0, f"upgrade run failed: {second.returncode} {second.stderr}"
    probes = list(installer.tool_dir.rglob(Path(UPGRADE_PROBE).name))
    assert probes, "the changed wheel was not installed"


def test_installer_removes_skill_files_the_dist_no_longer_ships(
    fake_sign: "FakeSign", installer: Installer, board_url: str
) -> None:
    fake_sign.configure(box="testbox", kinds=["claude", "pi"])
    first = installer.run("--board", board_url)
    assert first.returncode == 0, f"first run failed: {first.returncode} {first.stderr}"
    retired = installer.skill_dir / "references" / "retired-commands.md"
    retired.write_text("an old command list\n")

    second = installer.run("--board", board_url)

    assert second.returncode == 0, f"second run failed: {second.returncode} {second.stderr}"
    assert not retired.exists(), f"{retired} was left behind"
    assert _tree(installer.skill_dir) == _tree(SKILL_SOURCE), "installed skill differs from skill/agent-board"


def test_installer_dry_run_changes_nothing(
    fake_sign: "FakeSign", installer: Installer, board_url: str
) -> None:
    fake_sign.configure(box="testbox", kinds=["claude", "pi"])

    run = installer.run("--board", board_url, "--dry-run")

    assert run.returncode == 0, f"dry run failed: {run.returncode} {run.stderr}"
    assert "uv tool install" in run.stdout, f"dry run did not report the install it would do: {run.stdout}"
    created = _snapshot(installer.owned_paths)
    assert not created, f"dry run changed {sorted(created)}"


def test_skill_frontmatter_name_matches_directory_and_description_under_1024() -> None:
    fields = _frontmatter(SKILL_SOURCE / "SKILL.md")

    assert fields.get("name") == SKILL_SOURCE.name, (
        f"skill name {fields.get('name')!r} != {SKILL_SOURCE.name!r}"
    )
    description = fields.get("description", "")
    assert 0 < len(description) <= DESCRIPTION_LIMIT, f"description is {len(description)} characters"
