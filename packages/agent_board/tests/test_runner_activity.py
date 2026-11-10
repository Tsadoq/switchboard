from typing import Any

from agent_board.client import BoardError, BoardUnreachable
from agent_board.runner.activity import REPORT_PATH, ActivityReporter, threads_of

_START = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> float:
        return self.now


class Board:
    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []
        self.failure: Exception | None = None

    def post(self, path: str, body: dict[str, Any]) -> None:
        assert path == REPORT_PATH, path
        if self.failure is not None:
            raise self.failure
        self.reports.append(body)


class Sessions:
    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.probed: list[str] = []

    def idle(self, session_id: str, timeout: int = 3) -> bool:
        self.probed.append(session_id)
        return session_id not in self.busy


class Rig:
    def __init__(self) -> None:
        self.clock, self.board, self.sessions = Clock(), Board(), Sessions()
        self.reporter = ActivityReporter(self.board, self.sessions, self.clock)


def test_a_delivery_is_reported_as_running_without_probing_a_turn_that_may_not_have_started() -> None:
    rig = Rig()
    reporter, board, sessions = rig.reporter, rig.board, rig.sessions

    reporter.delivered("s1", (34,))
    reporter.probe()

    assert sessions.probed == [], f"probed a session before its turn could start: {sessions.probed}"
    assert board.reports == [{"running": [{"threads": [34], "since": _START}], "ttl_seconds": 90}], (
        board.reports
    )


def test_an_idle_session_is_reported_finished_once_and_then_nothing_is_sent() -> None:
    rig = Rig()
    reporter, clock, board, sessions = rig.reporter, rig.clock, rig.board, rig.sessions
    reporter.delivered("s1", (34,))
    reporter.probe()
    clock.now += 20

    reporter.probe()
    clock.now += 60
    reporter.probe()

    assert board.reports[1:] == [{"running": [], "ttl_seconds": 90}], board.reports
    assert sessions.probed == ["s1"], f"kept probing a finished session: {sessions.probed}"


def test_a_busy_session_is_refreshed_before_its_report_expires_but_not_on_every_probe() -> None:
    rig = Rig()
    reporter, clock, board, sessions = rig.reporter, rig.clock, rig.board, rig.sessions
    sessions.busy.add("s1")
    reporter.delivered("s1", (34,))
    reporter.probe()

    for _ in range(4):
        clock.now += 20
        reporter.probe()

    assert len(board.reports) == 3, f"expected the first report plus refreshes every 30 s: {board.reports}"
    assert all(report["running"][0]["since"] == _START for report in board.reports), board.reports


def test_another_wake_into_a_running_session_adds_its_thread_and_keeps_the_start() -> None:
    rig = Rig()
    reporter, clock, board = rig.reporter, rig.clock, rig.board
    reporter.delivered("s1", (34,))
    clock.now += 5
    reporter.delivered("s1", (41, 34))

    reporter.probe()

    assert board.reports[-1]["running"] == [{"threads": [34, 41], "since": _START}], board.reports


def test_scheduled_wakes_run_with_no_threads() -> None:
    rig = Rig()
    reporter, board = rig.reporter, rig.board

    reporter.delivered("timer-session", ())
    reporter.probe()

    assert board.reports[-1]["running"] == [{"threads": [], "since": _START}], board.reports


def test_a_board_without_the_activity_route_switches_reporting_off() -> None:
    rig = Rig()
    reporter, clock, board = rig.reporter, rig.clock, rig.board
    board.failure = BoardError(404, "Not Found")
    reporter.delivered("s1", (34,))
    reporter.probe()
    board.failure = None

    clock.now += 60
    reporter.delivered("s2", (35,))
    reporter.probe()

    assert board.reports == [], f"kept reporting to a board that lacks the route: {board.reports}"


def test_an_unreachable_board_is_retried_on_the_next_probe() -> None:
    rig = Rig()
    reporter, clock, board = rig.reporter, rig.clock, rig.board
    board.failure = BoardUnreachable("down")
    reporter.delivered("s1", (34,))
    reporter.probe()
    board.failure = None

    clock.now += 1
    reporter.probe()

    assert board.reports == [{"running": [{"threads": [34], "since": _START}], "ttl_seconds": 90}], (
        board.reports
    )


def test_threads_come_from_wake_fields_or_older_boards_summaries() -> None:
    wakes: list[dict[str, Any]] = [
        {"reason": "mention", "thread_id": 34, "summary": 'pi@dev mentioned you in thread 34 "T"'},
        {"reason": "follow", "summary": 'pi@dev posted in thread 41 "T"'},
        {"reason": "timer", "summary": "scheduled wake, every 600 s"},
    ]

    assert threads_of(wakes) == (34, 41), threads_of(wakes)
