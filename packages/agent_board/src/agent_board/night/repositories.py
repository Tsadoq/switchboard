import re
import stat
import tomllib
from pathlib import Path
from typing import Any

from agent_board.night.environment import MIRROR_ROOT, REPOSITORY_POLICY
from agent_board.night.launch import EXIT_ASSIGNMENT, LaunchRefused, has_acl, tree_trusted, trusted_containers

_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_ENTRY_KEYS = frozenset(("mirror", "visibility", "principals"))


class MirrorPolicy:
    """Resolves a repository key only to a reviewed, root-managed bare mirror of a public repository.

    The policy file and every mirror are root-owned and unwritable by the night uid, so neither the board nor
    the agent can redirect a key. Private repositories need a reviewed credential decision first and refuse.
    """

    def __init__(
        self,
        path: Path = Path(REPOSITORY_POLICY),
        mirror_root: Path = Path(MIRROR_ROOT),
        owner: int = 0,
        top: Path = Path("/"),
    ) -> None:
        self._path = path
        self._mirror_root = mirror_root
        self._owner = owner
        self._top = top

    def resolve(self, repository_key: str, principal: str) -> Path:
        if _KEY.fullmatch(repository_key) is None:
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_key_invalid")
        entry = self._entries().get(repository_key)
        if not isinstance(entry, dict) or frozenset(entry) != _ENTRY_KEYS:
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_not_in_policy")
        if entry["visibility"] != "public":
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_not_public")
        principals = entry["principals"]
        if not isinstance(principals, list) or principal not in principals:
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_not_for_principal")
        mirror = self._mirror_root / f"{repository_key}.git"
        if entry["mirror"] != str(mirror):
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_mirror_outside_root")
        if not self._managed(mirror) or not (mirror / "HEAD").is_file() or not (mirror / "objects").is_dir():
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_mirror_untrusted")
        return mirror

    def _entries(self) -> dict[str, Any]:
        if not trusted_containers(self._path, self._owner, self._top):
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_policy_untrusted")
        try:
            metadata = self._path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self._owner
                or metadata.st_mode & 0o022
                or has_acl(self._path)
            ):
                raise LaunchRefused(EXIT_ASSIGNMENT, "repository_policy_untrusted")
            table = tomllib.loads(self._path.read_text())
        except FileNotFoundError:
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_policy_not_configured") from None
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_policy_unreadable") from None
        if frozenset(table) != {"repositories"} or not isinstance(table["repositories"], dict):
            raise LaunchRefused(EXIT_ASSIGNMENT, "repository_policy_malformed")
        return table["repositories"]

    def _managed(self, mirror: Path) -> bool:
        try:
            metadata = mirror.lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == self._owner
            and not metadata.st_mode & 0o022
            and trusted_containers(mirror, self._owner, self._top)
            and tree_trusted(mirror, self._owner)
        )
