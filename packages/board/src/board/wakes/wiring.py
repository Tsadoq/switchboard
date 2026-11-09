from fastapi import FastAPI

from board.db import Database
from board.events import EventHub
from board.forum.fanout import WakeRequested
from board.settings import Settings
from board.wakes import routes
from board.wakes.bus import WakeBus


def connect(app: FastAPI) -> None:
    db: Database = app.state.db
    events: EventHub = app.state.events
    settings: Settings = app.state.settings
    bus = WakeBus(db, settings.wake_lease_seconds, settings.human_digest_seconds)

    async def store_requested_wake(event: object) -> None:
        if isinstance(event, WakeRequested):
            await bus.emit(event.request)

    events.subscribe(store_requested_wake)
    routes.install(app, bus, db)
    app.state.wakes = bus
