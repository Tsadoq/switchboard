import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

ORIGIN = "FIXTURE_ONLY_V1"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
BODY_CAP = 4096


class InvalidRecord(ValueError):
    pass


def _require(condition: bool) -> None:
    if not condition:
        raise InvalidRecord("invalid_fixture_gate_record")


def _uuid(value: object, *, nonzero: bool = False) -> None:
    if not isinstance(value, str) or type(value) is not str:
        raise InvalidRecord("invalid_uuid")
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidRecord("invalid_uuid") from exc
    _require(canonical == value and (not nonzero or value != ZERO_UUID))


def _hash(value: object) -> None:
    _require(type(value) is str and re.fullmatch("[0-9a-f]{64}", value, re.ASCII) is not None)


def _boolean(value: object) -> None:
    _require(type(value) is bool)


@dataclass(frozen=True)
class Context:
    store_version: int
    policy_version: int
    generation: str
    origin: str
    supervisor_release_sha256: str

    def __post_init__(self) -> None:
        _require(type(self.store_version) is int and self.store_version == 1)
        _require(type(self.policy_version) is int and self.policy_version == 1)
        _require(type(self.origin) is str and self.origin == ORIGIN)
        _uuid(self.generation)
        _hash(self.supervisor_release_sha256)

    def reference(self, operation: str, launch: str) -> str:
        _uuid(operation)
        _uuid(launch)
        return f"g1:p1:{self.generation}:{operation}:{launch}:{self.supervisor_release_sha256}"


@dataclass(frozen=True)
class RuntimeOwner:
    operation: str
    launch: str
    run_id: str
    fence_sha256: str

    def __post_init__(self) -> None:
        _uuid(self.operation, nonzero=True)
        _uuid(self.launch, nonzero=True)
        _uuid(self.run_id)
        _hash(self.fence_sha256)


@dataclass(frozen=True)
class RuntimeSelector:
    owner: RuntimeOwner
    kind: str = "RUNTIME_OWNER_V1"

    def __post_init__(self) -> None:
        _require(type(self.owner) is RuntimeOwner)
        _require(type(self.kind) is str and self.kind == "RUNTIME_OWNER_V1")


@dataclass(frozen=True)
class BootstrapSelector:
    kind: str = "FIXTURE_BOOTSTRAP_SELECTOR_V1"
    operation: str = ZERO_UUID
    launch: str = ZERO_UUID
    run_id: None = None
    fence_sha256: None = None

    def __post_init__(self) -> None:
        _require(type(self.kind) is str and self.kind == "FIXTURE_BOOTSTRAP_SELECTOR_V1")
        _require(type(self.operation) is str and self.operation == ZERO_UUID)
        _require(type(self.launch) is str and self.launch == ZERO_UUID)
        _require(self.run_id is None and self.fence_sha256 is None)


@dataclass(frozen=True)
class TrustedAssertions:
    associated_exit_asserted: bool
    complete_exchange_asserted: bool
    worker_pid: int
    exit_code: int
    exchange: str = "GUARDED_SEQPACKET_V1"

    def __post_init__(self) -> None:
        _require(self.associated_exit_asserted is True and self.complete_exchange_asserted is True)
        _require(type(self.worker_pid) is int and 1 <= self.worker_pid <= 2147483647)
        _require(type(self.exit_code) is int and self.exit_code == 0)
        _require(type(self.exchange) is str and self.exchange == "GUARDED_SEQPACKET_V1")


@dataclass(frozen=True)
class ResultRecord:
    run_id: str
    fence_sha256: str
    state: str
    stop_requested: bool
    uncertain: bool

    def __post_init__(self) -> None:
        _uuid(self.run_id)
        _hash(self.fence_sha256)
        _require(type(self.state) is str and self.state in ("stopping", "quarantined", "stopped"))
        _require(self.stop_requested is True)
        _boolean(self.uncertain)


@dataclass(frozen=True)
class TerminalResult:
    primary: str
    follow_up: str
    cleanup: str
    primary_changed: bool
    latch_changed: bool | None
    follow_up_changed: bool
    domain: str | None
    fault: str | None
    record: ResultRecord | None

    def validate(self, terminal: str, owner: RuntimeOwner) -> None:
        _boolean(self.primary_changed)
        _boolean(self.follow_up_changed)
        _require(type(self.cleanup) is str and self.cleanup == "CLOSED" and self.fault is None)
        _require(type(self.primary) is str and type(self.follow_up) is str)
        if terminal == "LATCH_SUCCESS":
            _require(self.primary == "COMMITTED" and self.follow_up == "NOT_ATTEMPTED")
            _boolean(self.latch_changed)
            _require(not self.latch_changed or self.primary_changed)
            _require(self.follow_up_changed is False and self.domain is None)
            _require(type(self.record) is ResultRecord)
            if self.record is not None:
                _require(self.record.run_id == owner.run_id)
                _require(self.record.fence_sha256 == owner.fence_sha256)
        elif terminal == "DOMAIN_REFUSED":
            _require(self.primary == "ROLLED_BACK" and self.follow_up == "COMMITTED")
            _require(self.primary_changed is False and self.latch_changed is None)
            _require(type(self.domain) is str and self.domain == "stale_fence")
            _require(self.record is None)
        else:
            raise InvalidRecord("invalid_terminal")


@dataclass(frozen=True)
class NormalAttestation:
    context: Context
    owner: RuntimeOwner
    terminal: str
    result: TerminalResult
    trusted_assertions: TrustedAssertions
    kind: str = "NORMAL_ATTESTATION_V1"

    def __post_init__(self) -> None:
        _require(type(self.context) is Context and type(self.owner) is RuntimeOwner)
        _require(type(self.kind) is str and self.kind == "NORMAL_ATTESTATION_V1")
        _require(type(self.terminal) is str and type(self.result) is TerminalResult)
        _require(type(self.trusted_assertions) is TrustedAssertions)
        self.result.validate(self.terminal, self.owner)


@dataclass(frozen=True)
class FixtureBootstrap:
    context: Context
    kind: str = "FIXTURE_BOOTSTRAP_V1"
    authorization: str = "SIMULATED_FIXTURE_SETUP_NOT_PRIVILEGED"
    operation: str = ZERO_UUID
    launch: str = ZERO_UUID

    def __post_init__(self) -> None:
        _require(type(self.context) is Context)
        _require(type(self.kind) is str and self.kind == "FIXTURE_BOOTSTRAP_V1")
        _require(
            type(self.authorization) is str and self.authorization == "SIMULATED_FIXTURE_SETUP_NOT_PRIVILEGED"
        )
        _require(type(self.operation) is str and self.operation == ZERO_UUID)
        _require(type(self.launch) is str and self.launch == ZERO_UUID)


RecordBody = NormalAttestation | FixtureBootstrap
Selector = RuntimeSelector | BootstrapSelector


def _object(value: object, fields: str) -> dict[str, Any]:
    _require(type(value) is dict)
    if not isinstance(value, dict):
        raise InvalidRecord("invalid_object")
    _require(set(value) == set(fields.split()))
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise InvalidRecord("invalid_json_constant")


def decode_selector(value: object) -> Selector:
    _require(type(value) is dict)
    if not isinstance(value, dict):
        raise InvalidRecord("invalid_selector")
    if value.get("kind") == "RUNTIME_OWNER_V1":
        data = _object(value, "kind owner")
        owner = RuntimeOwner(**_object(data["owner"], "operation launch run_id fence_sha256"))
        return RuntimeSelector(owner=owner, kind=data["kind"])
    return BootstrapSelector(**_object(value, "kind operation launch run_id fence_sha256"))


def encode_record(record: RecordBody) -> bytes:
    _require(type(record) in (NormalAttestation, FixtureBootstrap))
    body = json.dumps(asdict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    _require(len(body) <= BODY_CAP)
    return body


def decode_record(body: bytes, *, context: Context, owner: RuntimeOwner | None = None) -> RecordBody:
    _require(type(body) is bytes and len(body) <= BODY_CAP)
    _require(type(context) is Context and (owner is None or type(owner) is RuntimeOwner))
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidRecord("invalid_json") from exc
    _require(type(value) is dict)
    if not isinstance(value, dict):
        raise InvalidRecord("invalid_record")
    ctx = Context(
        **_object(
            value.get("context"),
            "store_version policy_version generation origin supervisor_release_sha256",
        )
    )
    _require(ctx == context)
    record: RecordBody
    if value.get("kind") == "FIXTURE_BOOTSTRAP_V1":
        data = _object(value, "kind context authorization operation launch")
        _require(owner is None)
        record = FixtureBootstrap(
            context=ctx,
            kind=data["kind"],
            authorization=data["authorization"],
            operation=data["operation"],
            launch=data["launch"],
        )
    else:
        data = _object(value, "kind context owner terminal result trusted_assertions")
        parsed_owner = RuntimeOwner(**_object(data["owner"], "operation launch run_id fence_sha256"))
        _require(owner is None or parsed_owner == owner)
        result_data = _object(
            data["result"],
            "primary follow_up cleanup primary_changed latch_changed follow_up_changed domain fault record",
        ).copy()
        if result_data["record"] is not None:
            result_data["record"] = ResultRecord(
                **_object(result_data["record"], "run_id fence_sha256 state stop_requested uncertain")
            )
        record = NormalAttestation(
            context=ctx,
            owner=parsed_owner,
            terminal=data["terminal"],
            result=TerminalResult(**result_data),
            trusted_assertions=TrustedAssertions(
                **_object(
                    data["trusted_assertions"],
                    "associated_exit_asserted complete_exchange_asserted worker_pid exit_code exchange",
                )
            ),
            kind=data["kind"],
        )
    _require(encode_record(record) == body)
    return record
