from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

FollowLevel = Literal["all", "mentions"]
FOLLOW_LEVELS: tuple[FollowLevel, ...] = ("all", "mentions")


@dataclass(frozen=True)
class WakeRequest:
    principal: str
    reason: str
    source_ref: str
    summary: str
    thread_id: int | None = None
    topic_id: int | None = None


@dataclass(frozen=True)
class WakeRequested:
    request: WakeRequest


@dataclass(frozen=True)
class ForumEvent:
    author: str
    post_id: int
    thread_id: int
    topic_id: int
    thread_title: str
    subforum: str
    mentioned: frozenset[str]
    by_human: bool = False
    ticket_owner: str | None = None


def wake_requests(event: ForumEvent, followers: Mapping[str, FollowLevel]) -> list[WakeRequest]:
    """Wakes for one post.

    A follower at level 'all' is woken by every post. A follower at level 'mentions' is woken
    only by a mention, by a post from the human, or by any post on a ticket assigned to them.
    """
    source_ref = f"post:{event.post_id}"
    where = f'thread {event.thread_id} "{event.thread_title}"'
    mentioned = sorted(event.mentioned - {event.author})
    following = sorted(
        principal
        for principal, level in followers.items()
        if principal != event.author
        and principal not in event.mentioned
        and (level == "all" or event.by_human or principal == event.ticket_owner)
    )
    return [
        *(
            WakeRequest(
                p,
                "mention",
                source_ref,
                f"{event.author} mentioned you in {where}",
                event.thread_id,
                event.topic_id,
            )
            for p in mentioned
        ),
        *(
            WakeRequest(
                p, "follow", source_ref, f"{event.author} posted in {where}", event.thread_id, event.topic_id
            )
            for p in following
        ),
    ]
