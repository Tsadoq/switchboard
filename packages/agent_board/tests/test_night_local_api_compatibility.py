import copy
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night.emission_lock import EmissionLock, EmissionLockError
from agent_board.night_state import StateError, check_private_file

if TYPE_CHECKING:
    from packages.agent_board.tests.night_local_api_fixture import Case, SQLFault, run, setup, validate
else:
    from night_local_api_fixture import Case, SQLFault, run, setup, validate

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="non-root compatibility fixture")


@pytest.fixture
def case() -> Iterator[Case]:
    with tempfile.TemporaryDirectory(prefix="night-api-", dir=Path.home()) as directory:
        yield setup(Path(directory))


def test_actual_private_paths_and_real_apis_succeed_without_metadata_mocks(case: Case) -> None:
    check_private_file(case.journal)
    check_private_file(case.lock)
    observed = run(case)
    found = observed.reported["result"]
    assert found["primary"] == "COMMITTED" and found["expiry"] == "NOT_ATTEMPTED"
    assert found["cleanup"] == "CLOSED" and found["record_state"] == "stopping"
    assert found["primary_changed"] is True and found["latch_changed"] is True
    assert found["fault"] is None and found["domain"] is None
    assert observed.current == ("stopping", 1, 1, 0, None)
    assert observed.exited["kind"] == "owned_child_waitpid" and observed.exited["code"] == 0
    assert observed.reported["journal_fds_after_api"] == []
    assert len(observed.reported["coordinator_fds_after_api"]) == 1
    assert len(observed.reported["sql_operations"]) <= 13


def test_idempotence_in_separate_exec_workers(case: Case) -> None:
    first, second = run(case), run(case)
    assert first.current == second.current
    assert second.reported["result"]["primary"] == "COMMITTED"
    assert second.reported["result"]["primary_changed"] is False
    assert second.reported["result"]["latch_changed"] is False
    assert first.exited["pid"] != second.exited["pid"]


@pytest.mark.parametrize("expired", [False, True])
def test_real_stale_fence_followup_has_separate_effects(expired: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="night-api-", dir=Path.home()) as directory:
        case = setup(Path(directory), expired=expired)
        observed = run(case, mode="stale")
    found = observed.reported["result"]
    assert found["primary"] == "ROLLED_BACK" and found["primary_changed"] is False
    assert found["expiry"] == "COMMITTED" and found["expiry_changed"] is expired
    assert found["cleanup"] == "CLOSED" and found["domain"] == "stale_fence"
    assert found["record_state"] is None and found["fault"] is None
    assert observed.current == (
        ("quarantined", 1, 1, 0, "deadline_passed") if expired else ("starting", 1, 0, 0, None)
    )
    assert len(observed.reported["sql_operations"]) <= 20


@pytest.mark.parametrize(
    "faults,primary,cleanup,state,changed",
    [
        ((SQLFault("COMMIT", 1, "before"),), "UNKNOWN", "CLOSED", "starting", None),
        ((SQLFault("COMMIT", 1, "after"),), "UNKNOWN", "CLOSED", "stopping", None),
        ((SQLFault("CLOSE", 1, "before"),), "COMMITTED", "UNCERTAIN", "stopping", True),
        ((SQLFault("CLOSE", 1, "after"),), "COMMITTED", "UNCERTAIN", "stopping", True),
        (
            (SQLFault("COMMIT", 1, "before"), SQLFault("CLOSE", 1, "before")),
            "UNKNOWN",
            "UNCERTAIN",
            "starting",
            None,
        ),
        (
            (SQLFault("COMMIT", 1, "after"), SQLFault("CLOSE", 1, "before")),
            "UNKNOWN",
            "UNCERTAIN",
            "stopping",
            None,
        ),
        (
            (SQLFault("COMMIT", 1, "after"), SQLFault("CLOSE", 1, "after")),
            "UNKNOWN",
            "UNCERTAIN",
            "stopping",
            None,
        ),
        ((SQLFault("UPDATE", 1, "before"),), "ROLLED_BACK", "CLOSED", "starting", False),
        ((SQLFault("UPDATE", 1, "after"),), "ROLLED_BACK", "CLOSED", "starting", False),
        (
            (
                SQLFault("UPDATE", 1, "after"),
                SQLFault("ROLLBACK", 1, "before"),
                SQLFault("CLOSE", 1, "before"),
            ),
            "UNKNOWN",
            "UNCERTAIN",
            "starting",
            None,
        ),
    ],
)
def test_actual_latch_fault_knowledge_is_separate_from_post_exit_state(
    case: Case, faults: tuple[SQLFault, ...], primary: str, cleanup: str, state: str, changed: bool | None
) -> None:
    observed = run(case, faults=faults)
    found = observed.reported["result"]
    assert found["primary"] == primary and found["cleanup"] == cleanup
    assert found["primary_changed"] is changed
    assert observed.current[0] == state
    assert found["record_state"] is None
    assert observed.exited["code"] == 0


@pytest.mark.parametrize("side", ["before", "after"])
def test_failed_first_rollback_never_starts_followup(case: Case, side: str) -> None:
    observed = run(case, mode="stale", faults=(SQLFault("ROLLBACK", 1, side), SQLFault("CLOSE", 1, "before")))
    found = observed.reported["result"]
    assert found["primary"] == "UNKNOWN" and found["cleanup"] == "UNCERTAIN"
    assert found["expiry"] == "NOT_ATTEMPTED"
    assert observed.reported["sql_operations"].count("BEGIN") == 1
    assert observed.reported["journal_fds_after_api"]
    assert observed.current == ("starting", 1, 0, 0, None)


def test_actual_coordinator_releases_order_before_report_while_descriptor_retained(case: Case) -> None:
    observed = run(case, mode="pause")
    assert observed.probed_while_alive
    assert len(observed.reported["coordinator_fds_after_api"]) == 1
    assert observed.reported["journal_fds_after_api"] == []
    assert observed.current == ("stopping", 1, 1, 0, None)


def test_live_worker_can_retain_real_sqlite_handle_after_order_is_released(case: Case) -> None:
    observed = run(case, mode="pause", faults=(SQLFault("CLOSE", 1, "before"),))
    assert observed.probed_while_alive
    assert observed.reported["journal_fds_after_api"]
    assert observed.reported["result"]["cleanup"] == "UNCERTAIN"
    assert observed.reported["result"]["primary"] == "COMMITTED"
    assert observed.exited["code"] == 0 and observed.current[0] == "stopping"


@pytest.mark.parametrize("target", ["journal", "lock", "parent"])
def test_real_unsafe_permissions_are_not_forged_or_bypassed(case: Case, target: str) -> None:
    path = case.directory if target == "parent" else getattr(case, target)
    before = path.stat().st_mode & 0o777
    path.chmod(0o777 if target == "parent" else 0o666)
    try:
        with pytest.raises(StateError):
            check_private_file(case.journal if target != "lock" else case.lock)
        if target == "journal":
            observed = run(case)
            assert observed.reported["result"]["fault"] == "validation_or_path_failure"
            assert observed.reported["result"]["primary"] == "NOT_ATTEMPTED"
            assert observed.reported["sql_operations"] == []
            assert observed.current == ("starting", 1, 0, 0, None)
        else:
            with pytest.raises(ValueError):
                run(case)
    finally:
        path.chmod(before)


@pytest.mark.parametrize("target", ["journal", "lock"])
def test_actual_hardlink_checks_refuse(case: Case, target: str) -> None:
    path = getattr(case, target)
    link = case.directory / "second-link"
    os.link(path, link)
    try:
        with pytest.raises(StateError):
            check_private_file(path)
        if target == "lock":
            with pytest.raises(EmissionLockError):
                EmissionLock(path)
        else:
            observed = run(case)
            assert observed.reported["result"]["fault"] == "validation_or_path_failure"
            assert observed.reported["sql_operations"] == []
    finally:
        link.unlink()


@pytest.mark.parametrize("target", ["journal", "lock"])
def test_actual_symlink_checks_refuse(case: Case, target: str) -> None:
    path = getattr(case, target)
    retained = case.directory / "retained-original"
    path.rename(retained)
    path.symlink_to(retained)
    try:
        with pytest.raises(StateError):
            check_private_file(path)
        if target == "lock":
            with pytest.raises(EmissionLockError):
                EmissionLock(path)
        else:
            observed = run(case)
            assert observed.reported["result"]["fault"] == "validation_or_path_failure"
            assert observed.reported["sql_operations"] == []
    finally:
        path.unlink()
        retained.rename(path)


def test_actual_coordinator_refuses_replaced_inode(case: Case) -> None:
    coordinator = EmissionLock(case.lock)
    retained = case.directory / "retained-lock"
    case.lock.rename(retained)
    fd = os.open(case.lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with pytest.raises(EmissionLockError, match="lock_inode_changed"), coordinator.held():
            raise AssertionError("unexpected_replaced_entry")
    finally:
        case.lock.unlink()
        retained.rename(case.lock)
    assert run(case).reported["result"]["primary_changed"] is True


def test_production_coordinator_refuses_nested_entry_before_mutation(case: Case) -> None:
    first, second = EmissionLock(case.lock), EmissionLock(case.lock)
    with first.held(), pytest.raises(EmissionLockError, match="nested_lock"), second.held():
        raise AssertionError("unexpected_nested_entry")
    assert run(case).reported["result"]["primary_changed"] is True


@pytest.mark.parametrize(
    "field",
    [
        "kind",
        "run_id",
        "channel_fd",
        "entry_fds",
        "primary",
        "cleanup",
        "record_state",
        "domain",
        "fault",
        "changed",
        "operations",
    ],
)
def test_distinct_compatibility_schema_refuses_invalid_reports(case: Case, field: str) -> None:
    value = copy.deepcopy(run(case).reported)
    if field in {"kind", "run_id", "channel_fd", "entry_fds"}:
        value[field] = "wrong"
    elif field in {"primary", "cleanup", "record_state", "domain", "fault"}:
        value["result"][field] = "WRONG"
    elif field == "changed":
        value["result"]["primary_changed"] = 1
    else:
        value["sql_operations"] = ["CREATE"]
    with pytest.raises((ValueError, TypeError)):
        validate(value, case)
