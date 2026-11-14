import json
from dataclasses import asdict, replace
from typing import Any

import pytest
from agent_board.night.fixture_gate_dto import (
    BODY_CAP,
    ORIGIN,
    ZERO_UUID,
    BootstrapSelector,
    Context,
    FixtureBootstrap,
    InvalidRecord,
    NormalAttestation,
    ResultRecord,
    RuntimeOwner,
    RuntimeSelector,
    TerminalResult,
    TrustedAssertions,
    decode_record,
    decode_selector,
    encode_record,
)

CONTEXT = Context(1, 1, "10000000-0000-4000-8000-000000000000", ORIGIN, "a" * 64)
OWNER = RuntimeOwner(
    "20000000-0000-4000-8000-000000000000",
    "30000000-0000-4000-8000-000000000000",
    "40000000-0000-4000-8000-000000000000",
    "b" * 64,
)


def normal() -> NormalAttestation:
    return NormalAttestation(
        CONTEXT,
        OWNER,
        "LATCH_SUCCESS",
        TerminalResult(
            "COMMITTED",
            "NOT_ATTEMPTED",
            "CLOSED",
            False,
            False,
            False,
            None,
            None,
            ResultRecord(OWNER.run_id, OWNER.fence_sha256, "stopping", True, False),
        ),
        TrustedAssertions(True, True, 123, 0),
    )


def encoded(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@pytest.mark.parametrize("primary,latch", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("state", ["stopping", "quarantined", "stopped"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_success_table(primary: bool, latch: bool, state: str, uncertain: bool) -> None:
    original = normal()
    record = ResultRecord(OWNER.run_id, OWNER.fence_sha256, state, True, uncertain)
    result = replace(original.result, primary_changed=primary, latch_changed=latch, record=record)
    attestation = replace(original, result=result)
    assert decode_record(encode_record(attestation), context=CONTEXT, owner=OWNER) == attestation


@pytest.mark.parametrize("changed", [False, True])
def test_stale_fence_expiry_is_domain_refusal(changed: bool) -> None:
    result = TerminalResult(
        "ROLLED_BACK", "COMMITTED", "CLOSED", False, None, changed, "stale_fence", None, None
    )
    attestation = replace(normal(), terminal="DOMAIN_REFUSED", result=result)
    assert decode_record(encode_record(attestation), context=CONTEXT) == attestation
    with pytest.raises(InvalidRecord):
        replace(attestation, terminal="LATCH_SUCCESS")


@pytest.mark.parametrize(
    "field,value",
    [
        ("primary", "UNKNOWN"),
        ("primary", "ROLLED_BACK"),
        ("primary", "NOT_ATTEMPTED"),
        ("follow_up", "COMMITTED"),
        ("follow_up", "UNKNOWN"),
        ("cleanup", "UNCERTAIN"),
        ("cleanup", "cleanup_uncertain"),
        ("primary_changed", None),
        ("primary_changed", 0),
        ("latch_changed", True),
        ("latch_changed", None),
        ("latch_changed", 0),
        ("follow_up_changed", True),
        ("follow_up_changed", 0),
        ("domain", "stale_fence"),
        ("fault", "close_failed"),
        ("record", None),
    ],
)
def test_success_rejects_invalid_combinations(field: str, value: Any) -> None:
    data = asdict(normal())
    data["result"][field] = value
    with pytest.raises(InvalidRecord):
        decode_record(encoded(data), context=CONTEXT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("primary", "COMMITTED"),
        ("follow_up", "NOT_ATTEMPTED"),
        ("follow_up", "UNKNOWN"),
        ("cleanup", "UNCERTAIN"),
        ("primary_changed", True),
        ("latch_changed", False),
        ("follow_up_changed", None),
        ("follow_up_changed", 1),
        ("domain", None),
        ("domain", "invalid_selector"),
        ("fault", "rollback_failed"),
    ],
)
def test_domain_refusal_requires_known_complete_outcomes(field: str, value: Any) -> None:
    data = asdict(normal())
    data["terminal"] = "DOMAIN_REFUSED"
    data["result"] = {
        "primary": "ROLLED_BACK",
        "follow_up": "COMMITTED",
        "cleanup": "CLOSED",
        "primary_changed": False,
        "latch_changed": None,
        "follow_up_changed": True,
        "domain": "stale_fence",
        "fault": None,
        "record": None,
    }
    data["result"][field] = value
    with pytest.raises(InvalidRecord):
        decode_record(encoded(data), context=CONTEXT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("associated_exit_asserted", False),
        ("associated_exit_asserted", 1),
        ("complete_exchange_asserted", False),
        ("complete_exchange_asserted", 1),
        ("worker_pid", True),
        ("worker_pid", 0),
        ("worker_pid", -1),
        ("worker_pid", 2147483648),
        ("worker_pid", 1.0),
        ("exit_code", False),
        ("exit_code", 1),
        ("exchange", "EOF"),
    ],
)
def test_assertion_shape_not_truth(field: str, value: Any) -> None:
    data = asdict(normal())
    data["trusted_assertions"][field] = value
    with pytest.raises(InvalidRecord):
        decode_record(encoded(data), context=CONTEXT)


def test_assertions_are_unverified_caller_data() -> None:
    attestation = replace(normal(), trusted_assertions=TrustedAssertions(True, True, 2147483647, 0))
    decoded = decode_record(encode_record(attestation), context=CONTEXT)
    assert decoded == attestation
    assert decoded.context.origin == ORIGIN


@pytest.mark.parametrize(
    "field,value",
    [
        ("store_version", True),
        ("store_version", 1.0),
        ("policy_version", "1"),
        ("policy_version", 2),
        ("origin", "PRIVILEGED"),
        ("generation", "10000000000040008000000000000000"),
        ("generation", True),
        ("supervisor_release_sha256", "A" * 64),
        ("supervisor_release_sha256", "a" * 65),
    ],
)
def test_context_is_exact(field: str, value: Any) -> None:
    data = asdict(normal())
    data["context"][field] = value
    with pytest.raises(InvalidRecord):
        decode_record(encoded(data), context=CONTEXT)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("owner", "operation", ZERO_UUID),
        ("owner", "launch", ZERO_UUID),
        ("owner", "run_id", None),
        ("owner", "fence_sha256", None),
        ("record", "run_id", OWNER.operation),
        ("record", "fence_sha256", "c" * 64),
        ("record", "state", "starting"),
        ("record", "stop_requested", False),
        ("record", "stop_requested", 1),
        ("record", "uncertain", 0),
    ],
)
def test_owner_and_record_binding(section: str, field: str, value: Any) -> None:
    data = asdict(normal())
    target = data["owner"] if section == "owner" else data["result"]["record"]
    target[field] = value
    with pytest.raises(InvalidRecord):
        decode_record(encoded(data), context=CONTEXT, owner=OWNER)


@pytest.mark.parametrize("section", [None, "context", "owner", "result", "trusted_assertions"])
def test_unknown_and_missing_fields_refuse(section: str | None) -> None:
    for missing in (False, True):
        data = asdict(normal())
        target = data if section is None else data[section]
        if missing:
            target.pop(next(iter(target)))
        else:
            target["unexpected"] = True
        with pytest.raises(InvalidRecord):
            decode_record(encoded(data), context=CONTEXT)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{}",
        b"[]",
        b"null",
        b"\xff",
        b'{"kind":1,"kind":1}',
        b'{"context":{"store_version":1,"store_version":1}}',
        b'{"context":NaN}',
        b'{"context":Infinity}',
        b"[" * 1500 + b"]" * 1500,
        b" " * (BODY_CAP + 1),
    ],
)
def test_malformed_and_overcap_records(body: bytes) -> None:
    with pytest.raises(InvalidRecord):
        decode_record(body, context=CONTEXT)


def test_noncanonical_trailing_and_wrong_context_refuse() -> None:
    body = encode_record(normal())
    for invalid in (body + b" ", body + b"{}", json.dumps(asdict(normal())).encode()):
        with pytest.raises(InvalidRecord):
            decode_record(invalid, context=CONTEXT)
    with pytest.raises(InvalidRecord):
        decode_record(body, context=replace(CONTEXT, supervisor_release_sha256="c" * 64))
    with pytest.raises(InvalidRecord):
        decode_record(body, context=CONTEXT, owner=replace(OWNER, operation=OWNER.launch))


def test_bootstrap_is_a_distinct_nonprivileged_record() -> None:
    bootstrap = FixtureBootstrap(CONTEXT)
    body = encode_record(bootstrap)
    assert decode_record(body, context=CONTEXT) == bootstrap
    assert len(CONTEXT.reference(ZERO_UUID, ZERO_UUID)) == 181
    with pytest.raises(InvalidRecord):
        decode_record(body, context=CONTEXT, owner=OWNER)
    with pytest.raises(InvalidRecord):
        replace(bootstrap, authorization="PRIVILEGED")
    with pytest.raises(InvalidRecord):
        replace(bootstrap, operation=OWNER.operation)


def test_selector_tags_do_not_weaken_runtime_owner() -> None:
    bootstrap = BootstrapSelector()
    runtime = RuntimeSelector(OWNER)
    assert decode_selector(asdict(bootstrap)) == bootstrap
    assert decode_selector(asdict(runtime)) == runtime
    for field, value in (("run_id", OWNER.run_id), ("operation", OWNER.operation)):
        data = asdict(bootstrap)
        data[field] = value
        with pytest.raises(InvalidRecord):
            decode_selector(data)
    for field, runtime_value in (("operation", ZERO_UUID), ("launch", ZERO_UUID), ("run_id", None)):
        data = asdict(runtime)
        data["owner"][field] = runtime_value
        with pytest.raises(InvalidRecord):
            decode_selector(data)
    with pytest.raises(InvalidRecord):
        decode_selector({**asdict(runtime), "unexpected": False})
