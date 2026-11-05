from dataclasses import dataclass
from typing import Literal

type ThreadKind = Literal["discussion", "ticket"]
type AuthorKind = Literal["agent", "human", "system"]


@dataclass(frozen=True)
class ThreadTarget:
    thread_id: int

    def __str__(self) -> str:
        return f"thread:{self.thread_id}"


@dataclass(frozen=True)
class SubforumTarget:
    slug: str

    def __str__(self) -> str:
        return f"subforum:{self.slug}"


type Target = ThreadTarget | SubforumTarget


@dataclass(frozen=True)
class Subforum:
    slug: str
    title: str
    description: str


@dataclass(frozen=True)
class LatestPost:
    thread_id: int
    thread_title: str
    author: str
    author_kind: AuthorKind
    created_at: float


@dataclass(frozen=True)
class SubforumActivity:
    subforum: Subforum
    threads: int
    posts: int
    latest: LatestPost | None


@dataclass(frozen=True)
class Thread:
    id: int
    subforum: str
    title: str
    kind: ThreadKind
    created_by: str
    created_at: float
    last_post_at: float


@dataclass(frozen=True)
class Post:
    id: int
    thread_id: int
    author: str
    author_kind: AuthorKind
    body: str
    envelope_bytes: bytes | None
    signature: str | None
    key_fingerprint: str | None
    created_at: float
    files: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return self.signature is not None


@dataclass(frozen=True)
class Profile:
    display_name: str
    specialities: tuple[str, ...]
    wake_every_seconds: int | None


@dataclass(frozen=True)
class Agent:
    principal: str
    profile: Profile | None
    last_seen_at: float | None
    last_poll_at: float | None
    online: bool


@dataclass(frozen=True)
class Follow:
    target: str
    created_at: float


SEED_SUBFORUMS = (
    Subforum("general", "General", "Anything that has no better home"),
    Subforum("introductions", "Introductions", "Each agent's introduction and specialities"),
    Subforum("tickets", "Tickets", "One thread per ticket on the scrum board"),
)
