import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_EXPORT = (
    "export",
    "--no-dev",
    "--no-emit-workspace",
    "--format",
    "requirements-txt",
    "--no-header",
    "--no-annotate",
    "--locked",
)


@pytest.mark.parametrize(
    ("artifact", "scope"),
    [
        pytest.param("deploy/night-verifier-requirements.txt", ("--package", "agent-board"), id="verifier"),
        pytest.param("deploy/night-smoke-requirements.txt", ("--all-packages",), id="smoke"),
    ],
)
def test_root_install_requirements_match_the_lock(
    tmp_path: Path, artifact: str, scope: tuple[str, ...]
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to check the hash-pinned root install requirements"
    exported = tmp_path / "requirements.txt"

    subprocess.run(
        [uv, *_EXPORT[:1], *scope, *_EXPORT[1:], "--output-file", str(exported)],
        cwd=REPO,
        check=True,
        capture_output=True,
        timeout=120,
    )

    committed = (REPO / artifact).read_text()
    assert exported.read_text() == committed, f"{artifact} is stale; regenerate it from uv.lock"
    assert "--hash=sha256:" in committed, f"{artifact} lost its hashes"
