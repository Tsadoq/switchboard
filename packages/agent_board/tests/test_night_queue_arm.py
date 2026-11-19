import math
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import effective_queue as eq
from agent_board.night import queue_arm as qa
from agent_board.night.journal_latch import MutationOutcome
from agent_board.night.ledger import Ledger, LedgerError

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import (
        BINDING,
        CONNECT,
        EMPTY,
        RECEIPT,
        RUN,
        ack_for,
        queue_path,
        tables,
        usable,
    )
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_effective_queue import (
        BINDING,
        CONNECT,
        EMPTY,
        RECEIPT,
        RUN,
        ack_for,
        queue_path,
        tables,
        usable,
    )
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
MAX = 2**63 - 1
ARM = qa.ArmTuple(RUN, "30000000-0000-4000-8000-000000000000", "a" * 32, 1000.0, 2000.0)
LATER = replace(ARM, guard_invocation_id="b" * 32, deadline_boottime=1100.0)


def ok(result: qa.QueueArmResult) -> eq.ExpectedControl:
    assert result.refusal is None and result.fault is None and result.control is not None, result
    return result.control


def refused(result: qa.QueueArmResult, reason: qa.Refusal) -> None:
    assert result.refusal == reason, result
    assert result.control is None and result.stored is None


def publish(
    path: Path, control: eq.ExpectedControl, desired: qa.ArmTuple, prior: qa.ArmTuple | None
) -> qa.QueueArmResult:
    return qa.compare_and_set_queue_arm(path, expected=control, publication=qa.ArmPublication(desired, prior))


def arms(path: Path) -> list[tuple[Any, ...]]:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        return conn.execute("SELECT * FROM arms ORDER BY run_id").fetchall()


def armed(path: Path) -> eq.ExpectedControl:
    return ok(publish(path, EMPTY, ARM, None))


def test_insert_then_replace_advance_only_the_revision(queue_path: Path) -> None:
    inserted = publish(queue_path, EMPTY, ARM, None)
    first = ok(inserted)
    assert (
        inserted.mutation == MutationOutcome.COMMITTED and inserted.changed is True and inserted.stored == ARM
    )
    assert first == replace(EMPTY, revision=1)
    assert arms(queue_path) == [(RUN, ARM.guard_boot_id, "a" * 32, 1000.0, 2000.0)]

    replaced = publish(queue_path, first, LATER, ARM)
    assert ok(replaced) == replace(EMPTY, revision=2) and replaced.stored == LATER
    assert arms(queue_path) == [(RUN, ARM.guard_boot_id, "b" * 32, 1100.0, 2000.0)]
    assert tables(queue_path)["receipts"] == [] and tables(queue_path)["identity"] == [
        (1, EMPTY.journal_id, 0)
    ]


def test_an_exact_match_rolls_back_and_keeps_the_token(queue_path: Path) -> None:
    control = armed(queue_path)
    before = tables(queue_path)

    same = publish(queue_path, control, ARM, ARM)

    assert ok(same) == control and same.mutation == MutationOutcome.ROLLED_BACK and same.changed is False
    assert same.stored == ARM and tables(queue_path) == before


@pytest.mark.parametrize(
    ("setup", "desired", "prior"),
    [
        pytest.param(True, ARM, None, id="present-equals-desired-but-prior-none"),
        pytest.param(True, LATER, None, id="present-but-prior-none"),
        pytest.param(False, ARM, ARM, id="absent-but-prior-given"),
        pytest.param(True, LATER, replace(ARM, deadline_utc=2001.0), id="prior-differs-in-one-field"),
    ],
)
def test_anything_but_an_exact_prior_is_an_arm_conflict(
    queue_path: Path, setup: bool, desired: qa.ArmTuple, prior: qa.ArmTuple | None
) -> None:
    control = armed(queue_path) if setup else EMPTY
    before = tables(queue_path)
    result = publish(queue_path, control, desired, prior)
    refused(result, qa.Refusal.ARM_CONFLICT)
    assert result.mutation == MutationOutcome.ROLLED_BACK and tables(queue_path) == before


@pytest.mark.parametrize(
    "field", ["guard_boot_id", "guard_invocation_id", "deadline_boottime", "deadline_utc"]
)
def test_every_field_takes_part_in_the_compare_and_set(queue_path: Path, field: str) -> None:
    control = armed(queue_path)
    changed: Any = {
        "guard_boot_id": "30000000-0000-4000-8000-000000000001",
        "guard_invocation_id": "c" * 32,
        "deadline_boottime": 999.0,
        "deadline_utc": 1999.0,
    }[field]
    refused(publish(queue_path, control, LATER, replace(ARM, **{field: changed})), qa.Refusal.ARM_CONFLICT)
    assert ok(publish(queue_path, control, replace(ARM, **{field: changed}), ARM)).revision == 2


def test_an_active_queue_refuses_every_arm_call_before_reading_it(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = armed(queue_path)
    active = usable(eq.publish_ordinary(queue_path, expected=control, publication=BINDING))
    before = tables(queue_path)
    tape = Tape(monkeypatch)

    for prior in (ARM, None):
        refused(publish(queue_path, active, ARM if prior else LATER, prior), qa.Refusal.QUEUE_NOT_EMPTY)

    assert all(qa.ARM_QUERY != sql for sql in tape.attempts)
    assert tables(queue_path) == before


def test_the_arm_token_and_the_queue_token_are_one_lifecycle(queue_path: Path) -> None:
    control = armed(queue_path)
    assert eq.observe_ordinary(queue_path, expected=EMPTY).refusal == eq.Refusal.CONTROL_MISMATCH
    active = usable(eq.publish_ordinary(queue_path, expected=control, publication=BINDING))
    acked = usable(
        eq.acknowledge_ordinary(queue_path, expected=active, ack=ack_for(active, RECEIPT, "running"))
    )
    refused(publish(queue_path, control, LATER, ARM), qa.Refusal.CONTROL_MISMATCH)
    rearmed = ok(publish(queue_path, acked, LATER, ARM))
    assert rearmed == replace(acked, revision=acked.revision + 1)
    assert eq.observe_ordinary(queue_path, expected=acked).refusal == eq.Refusal.CONTROL_MISMATCH
    assert usable(eq.observe_ordinary(queue_path, expected=rearmed)) == rearmed


def test_an_arm_for_a_run_with_no_binding_is_admitted_as_metadata_only(queue_path: Path) -> None:
    other = replace(ARM, run_id="a0000000-0000-4000-8000-000000000000")
    assert ok(publish(queue_path, EMPTY, other, None)).revision == 1
    assert tables(queue_path)["bindings"] == []


def test_signed_zero_deadlines_are_stored_as_positive_zero(queue_path: Path) -> None:
    zero = replace(ARM, deadline_boottime=-0.0, deadline_utc=0.0)
    result = publish(queue_path, EMPTY, zero, None)
    assert result.stored is not None
    assert math.copysign(1.0, result.stored.deadline_boottime) == 1.0
    assert math.copysign(1.0, arms(queue_path)[0][3]) == 1.0


def test_exhaustion_refuses_a_change_but_not_an_exact_match(queue_path: Path) -> None:
    control = armed(queue_path)
    with closing(CONNECT(queue_path, isolation_level=None)) as conn:
        conn.execute(f"UPDATE effective_queue SET revision={MAX}")
    at_max = replace(control, revision=MAX)
    assert ok(publish(queue_path, at_max, ARM, ARM)) == at_max
    refused(publish(queue_path, at_max, LATER, ARM), qa.Refusal.EXHAUSTED)


def test_a_maximum_counter_alone_does_not_forbid_an_arm_change(queue_path: Path) -> None:
    with closing(CONNECT(queue_path, isolation_level=None)) as conn:
        conn.execute(f"UPDATE identity SET acked_counter={MAX}")
    assert ok(publish(queue_path, replace(EMPTY, acked_counter=MAX), ARM, None)).acked_counter == MAX


class Text(str):
    pass


class Real(float):
    pass


def tamper(value: Any, **changes: Any) -> Any:
    for name, changed in changes.items():
        object.__setattr__(value, name, changed)
    return value


def deleted(value: Any, name: str) -> Any:
    object.__delattr__(value, name)
    return value


@pytest.mark.parametrize(
    "publication",
    [
        pytest.param(qa.ArmPublication(replace(ARM, run_id=Text(RUN)), None), id="run-subclass"),
        pytest.param(
            qa.ArmPublication(replace(ARM, run_id=RUN.upper().replace("0", "A")), None),
            id="run-not-canonical",
        ),
        pytest.param(
            qa.ArmPublication(replace(ARM, guard_invocation_id="A" * 32), None), id="invocation-uppercase"
        ),
        pytest.param(
            qa.ArmPublication(replace(ARM, guard_invocation_id="a" * 31), None), id="invocation-short"
        ),
        pytest.param(qa.ArmPublication(replace(ARM, deadline_boottime=1000), None), id="deadline-int"),
        pytest.param(qa.ArmPublication(replace(ARM, deadline_boottime=True), None), id="deadline-bool"),
        pytest.param(
            qa.ArmPublication(replace(ARM, deadline_utc=Real(2000.0)), None), id="deadline-subclass"
        ),
        pytest.param(qa.ArmPublication(replace(ARM, deadline_utc=float("nan")), None), id="deadline-nan"),
        pytest.param(qa.ArmPublication(replace(ARM, deadline_utc=float("inf")), None), id="deadline-inf"),
        pytest.param(qa.ArmPublication(replace(ARM, deadline_utc=-1.0), None), id="deadline-negative"),
        pytest.param(
            qa.ArmPublication(LATER, replace(ARM, run_id="a0000000-0000-4000-8000-000000000000")),
            id="prior-other-run",
        ),
        pytest.param(qa.ArmPublication(ARM, object()), id="prior-not-a-tuple"),  # type: ignore[arg-type]
        pytest.param(tamper(qa.ArmPublication(ARM, None), desired=object()), id="desired-tampered"),
        pytest.param(deleted(qa.ArmPublication(ARM, None), "expected_prior"), id="prior-deleted"),
        pytest.param(qa.ArmPublication(deleted(replace(ARM), "deadline_utc"), None), id="field-deleted"),
        pytest.param(object(), id="not-a-publication"),
    ],
)
def test_malformed_input_is_invalid_before_any_sql(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, publication: Any
) -> None:
    tape = Tape(monkeypatch)
    result = qa.compare_and_set_queue_arm(queue_path, expected=EMPTY, publication=publication)
    tape.dispose()
    refused(result, qa.Refusal.INVALID_INPUT)
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and tape.attempts == []


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "UPDATE arms SET guard_invocation_id='A'||substr(guard_invocation_id,2)", id="invocation"
        ),
        pytest.param("UPDATE arms SET deadline_utc='soon'", id="deadline-text"),
        pytest.param("UPDATE arms SET deadline_utc=-1.0", id="deadline-negative"),
        pytest.param("UPDATE arms SET guard_boot_id='not-a-uuid'", id="boot"),
    ],
)
def test_a_malformed_current_arm_refuses_before_comparing_the_prior(queue_path: Path, statement: str) -> None:
    control = armed(queue_path)
    with closing(CONNECT(queue_path, isolation_level=None)) as conn:
        conn.execute(statement)
    before = tables(queue_path)
    refused(publish(queue_path, control, LATER, ARM), qa.Refusal.ROW_REFUSED)
    assert tables(queue_path) == before


def test_the_legacy_ledger_still_refuses_version_4(queue_path: Path) -> None:
    armed(queue_path)
    before = queue_path.read_bytes()
    with pytest.raises(LedgerError, match="unsupported_ledger_version"):
        Ledger(queue_path)
    assert queue_path.read_bytes() == before


type Prepare = Callable[[Path], eq.ExpectedControl]
type Operation = Callable[[Path, eq.ExpectedControl], qa.QueueArmResult]
TRANSITIONS: list[tuple[str, int, int, Prepare, Operation]] = [
    ("insert", 16, 16, lambda p: EMPTY, lambda p, c: publish(p, c, ARM, None)),
    ("replace", 16, 16, armed, lambda p, c: publish(p, c, LATER, ARM)),
    ("exact-match", 11, 11, armed, lambda p, c: publish(p, c, ARM, ARM)),
    ("conflict", 11, 11, armed, lambda p, c: publish(p, c, LATER, None)),
    (
        "active",
        10,
        10,
        lambda p: usable(eq.publish_ordinary(p, expected=armed(p), publication=BINDING)),
        lambda p, c: publish(p, c, LATER, ARM),
    ),
    ("control-mismatch", 10, 10, armed, lambda p, c: publish(p, replace(c, revision=7), LATER, ARM)),
]


@pytest.mark.parametrize(
    ("count", "ceiling", "prepare", "operation"),
    [
        pytest.param(count, ceiling, prepare, operation, id=name)
        for name, count, ceiling, prepare, operation in TRANSITIONS
    ],
)
def test_each_branch_has_a_fixed_schedule_within_its_ceiling(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    ceiling: int,
    prepare: Prepare,
    operation: Operation,
) -> None:
    control = prepare(queue_path)
    tape = Tape(monkeypatch)
    result = operation(queue_path, control)
    tape.dispose()
    assert result.fault is None and tape.closes == 1
    assert len(tape.native) == len(tape.attempts) == count <= ceiling
    tape.reconcile()
