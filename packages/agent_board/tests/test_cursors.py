import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from agent_board import cursors
from agent_board.cursors import CursorCorrupt, CursorError, CursorUnsafe, consumer_id, hand_off, locked

OLD, NEW = "a" * 32, "b" * 32


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    path = tmp_path / "cursors"
    path.mkdir(mode=0o700)
    return path


def test_a_position_only_moves_up_and_each_thread_has_its_own_file(directory: Path) -> None:
    with locked(directory, OLD) as positions:
        positions.advance(31, 100)
        positions.advance(31, 90)
        positions.advance(54, 7)
        assert positions.get(31) == 100 and positions.get(54) == 7 and positions.get(9) is None
    assert sorted(path.name for path in (directory / OLD).iterdir()) == ["31.json", "54.json"]


@pytest.mark.parametrize("value", [None, "", "../x", "a/b", "a" * 65, "x y", "é"])
def test_a_consumer_id_outside_the_pattern_is_refused(value: str | None) -> None:
    with pytest.raises(CursorError):
        consumer_id(value)


@pytest.mark.parametrize(
    "text", ["not json", '{"v": 1}', '{"v": 2, "post": 3}', '{"v": 1, "post": -1}', "[]"]
)
def test_an_unreadable_cursor_refuses_and_is_never_reset(directory: Path, text: str) -> None:
    with locked(directory, OLD) as positions:
        positions.advance(31, 5)
        (directory / OLD / "31.json").write_text(text)
        with pytest.raises(CursorCorrupt):
            positions.get(31)
        with pytest.raises(CursorCorrupt):
            positions.advance(31, 9)
    assert (directory / OLD / "31.json").read_text() == text


def test_recovery_moves_only_a_corrupt_cursor_aside(directory: Path) -> None:
    with locked(directory, OLD) as positions:
        positions.advance(31, 5)
        with pytest.raises(CursorError, match="valid"):
            positions.set_aside(31)
        (directory / OLD / "31.json").write_text("garbage")
        aside = positions.set_aside(31)
        assert aside.read_text() == "garbage" and positions.get(31) is None


def _symlinked_cursor(directory: Path) -> None:
    (directory / OLD).mkdir(mode=0o700)
    target = directory / "elsewhere.json"
    target.write_text('{"v": 1, "post": 1}')
    target.chmod(0o600)
    (directory / OLD / "31.json").symlink_to(target)


def _shared_cursor(directory: Path) -> None:
    (directory / OLD).mkdir(mode=0o700)
    path = directory / OLD / "31.json"
    path.write_text('{"v": 1, "post": 1}')
    path.chmod(0o644)


@pytest.mark.parametrize("spoil", [_symlinked_cursor, _shared_cursor])
def test_an_unsafe_cursor_is_never_followed_repaired_or_recovered(
    directory: Path, spoil: Callable[[Path], None]
) -> None:
    spoil(directory)

    with locked(directory, OLD) as positions:
        with pytest.raises(CursorUnsafe):
            positions.get(31)
        with pytest.raises(CursorUnsafe):
            positions.set_aside(31)


def test_a_symlinked_lock_is_refused(directory: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.lock"
    target.write_text("")
    (directory / f"{OLD}.lock").symlink_to(target)

    with pytest.raises(CursorUnsafe), locked(directory, OLD):
        pass


def test_a_lock_replaced_while_waiting_is_refused(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = cursors._check_lock
    calls = {"n": 0}

    def replace_after_open(path: Path, fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            path.unlink()
            path.write_text("")
            path.chmod(0o600)
        real(path, fd)

    monkeypatch.setattr(cursors, "_check_lock", replace_after_open)

    with pytest.raises(CursorUnsafe), locked(directory, OLD):
        pass


def test_a_held_lock_makes_a_second_reader_wait_then_refuse(directory: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        with locked(directory, OLD):
            entered.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    entered.wait(5)
    try:
        with pytest.raises(CursorError, match="busy"), locked(directory, OLD, timeout=0.2):
            pass
    finally:
        release.set()
        holder.join()


def test_a_hand_off_retires_first_and_copies_only_what_the_successor_lacks(directory: Path) -> None:
    with locked(directory, OLD) as old:
        old.advance(31, 100)
        old.advance(54, 7)
    with locked(directory, NEW) as new:
        new.advance(54, 9)

    copied = hand_off(directory, OLD, NEW)

    assert copied == {31: 100}
    with locked(directory, NEW) as new:
        assert new.get(31) == 100 and new.get(54) == 9
    with locked(directory, OLD) as old:
        assert old.retired


def test_a_crash_after_retiring_but_before_copying_leaves_the_successor_empty(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with locked(directory, OLD) as old:
        old.advance(31, 100)

    def crash(self: cursors.Cursors, thread_id: int, post: int) -> None:
        raise OSError("power lost")

    monkeypatch.setattr(cursors.Cursors, "advance", crash)

    with pytest.raises(OSError):
        hand_off(directory, OLD, NEW)

    monkeypatch.undo()
    with locked(directory, OLD) as old:
        assert old.retired, "the old consumer could still advance after the crash"
    with locked(directory, NEW) as new:
        assert new.get(31) is None


def test_state_files_are_private(directory: Path) -> None:
    with locked(directory, OLD) as positions:
        positions.advance(31, 1)
    for path in (directory / f"{OLD}.lock", directory / OLD / "31.json"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert (directory / OLD).stat().st_mode & 0o777 == 0o700
    assert not any(name.endswith(".tmp") for name in os.listdir(directory / OLD))


def _symlinked_consumer(directory: Path) -> None:
    real = directory.parent / "elsewhere"
    real.mkdir(mode=0o700)
    (real / "31.json").write_text('{"v": 1, "post": 1000}')
    (real / "31.json").chmod(0o600)
    (directory / OLD).symlink_to(real, target_is_directory=True)


def _shared_consumer(directory: Path) -> None:
    (directory / OLD).mkdir(mode=0o755)
    (directory / OLD).chmod(0o755)
    (directory / OLD / "31.json").write_text('{"v": 1, "post": 1000}')
    (directory / OLD / "31.json").chmod(0o600)


@pytest.mark.parametrize("spoil", [_symlinked_consumer, _shared_consumer])
def test_an_unsafe_consumer_directory_refuses_before_any_state_is_read(
    directory: Path, spoil: Callable[[Path], None]
) -> None:
    spoil(directory)

    with locked(directory, OLD) as positions:
        for read in (lambda: positions.get(31), lambda: positions.retired, positions.threads):
            with pytest.raises(CursorUnsafe):
                read()
    with pytest.raises(CursorUnsafe):
        hand_off(directory, OLD, NEW)


def test_a_lock_replaced_while_a_reader_holds_it_stops_the_commit(directory: Path) -> None:
    with locked(directory, OLD) as positions:
        positions.advance(31, 5)
        lock = directory / f"{OLD}.lock"
        lock.unlink()
        lock.write_text("")
        lock.chmod(0o600)
        with pytest.raises(CursorUnsafe):
            positions.verify()
        with pytest.raises(CursorUnsafe):
            positions.advance(31, 9)
    with locked(directory, OLD) as positions:
        assert positions.get(31) == 5


def test_two_recoveries_in_the_same_second_keep_both_pieces_of_evidence(directory: Path) -> None:
    with locked(directory, OLD) as positions:
        positions.advance(31, 5)
        path = directory / OLD / "31.json"
        path.write_text("first garbage")
        first = positions.set_aside(31)
        positions.advance(31, 6)
        path.write_text("second garbage")
        second = positions.set_aside(31)

    assert first != second
    assert first.read_text() == "first garbage" and second.read_text() == "second garbage"
