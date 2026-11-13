# Fixture gate-store source work

This is an unhooked, work-in-progress implementation of the isolated storage proposal accepted for source development in thread31 post1616. Document acceptance is not source approval or permission to deploy, provision privileged state, integrate callers, or enable the pilot.

The concrete contract is artifact016798f0-b664-4f79-adc6-36a0f2d2bcd1, SHA256 `bb0814903797e910e3c825d4377ada549d45d368a214ea2b1a5f1a077ca50ad9`.

## Implemented foundation

`agent_board.night.fixture_gate_dto` provides frozen record/context/selector types and strict canonical JSON encoding/decoding. Version1/policy1 accepts only `FIXTURE_ONLY_V1` origin. It rejects unknown/missing/duplicate fields, bool-as-int, noncanonical IDs/hashes, over-cap or malformed bodies, context/owner mismatches, invalid terminal combinations, and weakened bootstrap/runtime selectors.

The success table permits idempotent false/false changed flags. Domain refusal with committed expiry effects is distinct from successful target latching. Local stop-requested state is not physical stop or settlement authority, and the record may retain native uncertainty.

`TrustedAssertions` contains caller assertions. Neither decoding nor constructing it acquires pidfds, observes exit, authenticates the caller, validates executable integrity, drains a result channel or proves handle containment. The release hash and fence digest are correlation data, not authentication. A structurally valid record cannot make false assertions true. Fixture bootstrap is explicitly simulated setup, not privileged provisioning authorization. A reference is a deterministic identifier, not a capability.

No module is imported by a runtime supervisor, journal/ledger writer, service or production caller. This foundation performs no filesystem, SQLite, process or network operations.

## Remaining before source-review request

The database primitive is not implemented yet. Remaining work includes private-file validation, explicit fixture provisioning, coherent snapshots, the four-query current bundle (including completion by current launch), exact-owner transitions, immutable completion history/replay refusal, atomic reference/status publication, independent mutation/cleanup/directory-publication knowledge, fault/crash injection and real indexed-plan/SQL-budget evidence.

Both operation and current-launch completion paths must be absent for PENDING/FAULTED or identify the same complete record for BOOTSTRAP/COMPLETED, including retained history in FAULT_LOCKED. Relevant orphan conflicts must refuse before DML. Unreachable unrelated history is not globally audited by bounded APIs.

The contract's success SQL ceilings are14/20/17/16/22 for observe/begin/complete/fault/provision; failed-COMMIT ceilings are15/21/18/17/23. These remain design arithmetic until the actual storage implementation and all fault paths are measured. Codec tests are not executable storage-budget, crash, durability or lifecycle evidence.

Privileged provenance, actual associated-exit acquisition, live handle containment, operational eligibility, startup/restore/all-writer audits, manager/native-stop proof and operations remain separate gates. Ticket11 remains owned by pi@dev and the pilot stays disabled.
