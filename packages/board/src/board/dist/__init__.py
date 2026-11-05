from pathlib import Path

INSTALL_SCRIPT = "install.sh"
PUBLIC_URL_PLACEHOLDER = b"@BOARD_PUBLIC_URL@"


class NotDistributed(LookupError):
    pass


class Distribution:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def file(self, name: str) -> Path:
        if "\0" in name:
            raise NotDistributed(name)
        path = (self._root / name).resolve()
        if not path.is_relative_to(self._root) or not path.is_file():
            raise NotDistributed(name)
        return path


def install_script(distribution: Distribution, public_url: str) -> bytes:
    script = distribution.file(INSTALL_SCRIPT).read_bytes()
    return script.replace(PUBLIC_URL_PLACEHOLDER, public_url.encode())
