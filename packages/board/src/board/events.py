from collections.abc import Awaitable, Callable

type Subscriber = Callable[[object], Awaitable[None]]


class EventHub:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    async def publish(self, event: object) -> None:
        for fn in self._subscribers:
            await fn(event)
