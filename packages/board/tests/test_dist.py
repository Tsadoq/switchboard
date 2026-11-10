import hashlib
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from board.app import create_app
from board.settings import Settings
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[3]
BUILD_DIST = REPO / "scripts" / "build-dist.sh"
INSTALLER_SOURCE = REPO / "installer"
SKILL_SOURCE = REPO / "skill" / "agent-board"
CHECKSUMS = "SHA256SUMS"
PUBLIC_URL = "https://board.test.invalid"
PLACEHOLDER = "@BOARD_PUBLIC_URL@"
WHEEL = re.compile(r"agent_board-[^/]+\.whl")
_BUILD_SECONDS = 120


def _listed(checksums: str) -> dict[str, str]:
    listed = {}
    for line in checksums.splitlines():
        digest, name = line.split(maxsplit=1)
        listed[name.removeprefix("*")] = digest
    return listed


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dist = tmp_path_factory.mktemp("build") / "dist"
    completed = subprocess.run(
        [str(BUILD_DIST), str(dist)], check=False, capture_output=True, text=True, timeout=_BUILD_SECONDS
    )
    assert completed.returncode == 0, f"build-dist.sh failed: {completed.stderr}"
    return dist


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def dist_client(built_dist: Path, state_dir: Path) -> Iterator[TestClient]:
    dist = state_dir / "dist"
    shutil.copytree(built_dist, dist)
    settings = Settings.from_env(
        {"BOARD_STATE_DIR": str(state_dir), "BOARD_DIST_DIR": str(dist), "BOARD_PUBLIC_URL": PUBLIC_URL}
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_install_script_and_checksums_are_served_consistently(dist_client: TestClient) -> None:
    text = dist_client.get(f"/dist/{CHECKSUMS}").text
    listed = _listed(text)
    assert len(listed) >= 3, f"checksum file too short: {text}"
    for name, digest in listed.items():
        served = dist_client.get(f"/dist/{name}")
        assert hashlib.sha256(served.content).hexdigest() == digest, f"dist checksum mismatch for {name}"

    script = dist_client.get("/install.sh").text
    snippet = next((line for line in script.splitlines() if "BOARD_URL_DEFAULT=" in line), script[:200])
    assert f'BOARD_URL_DEFAULT="{PUBLIC_URL}"' in script and PLACEHOLDER not in script, (
        f"placeholder not substituted: {snippet}"
    )


def test_build_dist_lists_what_the_installer_downloads(built_dist: Path) -> None:
    listed = set(_listed((built_dist / CHECKSUMS).read_text()))
    installer_files = {path.name for path in INSTALLER_SOURCE.iterdir() if path.is_file()}
    skill_files = {
        f"skill/agent-board/{path.relative_to(SKILL_SOURCE).as_posix()}"
        for path in SKILL_SOURCE.rglob("*")
        if path.is_file()
    }
    wheels = {name for name in listed if WHEEL.fullmatch(name)}

    assert len(wheels) == 1, f"expected exactly one agent_board wheel, listed {sorted(listed)}"
    assert listed - wheels == installer_files | skill_files, (
        f"dist layout differs from the installer's: {sorted(listed - wheels)}"
    )


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/dist/%2e%2e/board.sqlite3", id="encoded-dots"),
        pytest.param("/dist/..%2Fboard.sqlite3", id="encoded-slash"),
        pytest.param("/dist/{state_dir}/board.sqlite3", id="absolute"),
        pytest.param("/dist/install.sh%00", id="nul-byte"),
    ],
)
def test_dist_path_traversal_is_rejected(dist_client: TestClient, state_dir: Path, path: str) -> None:
    assert (state_dir / "board.sqlite3").is_file(), (
        "the traversal target does not exist, so nothing is tested"
    )

    r = dist_client.get(path.format(state_dir=state_dir))

    assert r.status_code == 404, f"{path} was not refused: {r.status_code} {r.text[:200]}"


def test_install_script_has_no_content_encoding(dist_client: TestClient) -> None:
    r = dist_client.get("/install.sh", headers={"Accept-Encoding": "gzip, deflate, br"})

    assert r.status_code == 200, f"install.sh not served: {r.status_code} {r.text[:200]}"
    assert "content-encoding" not in r.headers, f"install.sh was encoded: {r.headers['content-encoding']}"
    assert r.headers["content-type"].startswith("text/x-shellscript"), (
        f"install.sh served as {r.headers['content-type']}"
    )


@pytest.mark.parametrize("path", ["/install.sh", f"/dist/{CHECKSUMS}"])
def test_downloads_declare_their_length_and_forbid_caching(dist_client: TestClient, path: str) -> None:
    r = dist_client.get(path)

    assert r.status_code == 200, f"{path} not served: {r.status_code} {r.text[:200]}"
    assert r.headers.get("cache-control") == "no-store", f"{path} may be cached: {r.headers}"
    assert r.headers.get("content-length") == str(len(r.content)), f"{path} length not declared: {r.headers}"


@pytest.mark.parametrize("path", ["/install.sh", f"/dist/{CHECKSUMS}", "/dist/install.sh"])
def test_downloads_answer_head_like_get_without_a_body(dist_client: TestClient, path: str) -> None:
    got = dist_client.get(path)
    head = dist_client.head(path)

    assert head.status_code == 200, f"HEAD {path} refused: {head.status_code} {head.text[:200]}"
    assert head.content == b"", f"HEAD {path} sent a body of {len(head.content)} bytes"
    for header in ("content-type", "content-length", "cache-control"):
        assert head.headers.get(header) == got.headers.get(header), (
            f"HEAD {path} {header} is {head.headers.get(header)!r}, GET says {got.headers.get(header)!r}"
        )


def test_head_on_a_missing_dist_file_is_404(dist_client: TestClient) -> None:
    r = dist_client.head("/dist/no-such-file")

    assert r.status_code == 404, f"HEAD on a missing file answered {r.status_code}"
