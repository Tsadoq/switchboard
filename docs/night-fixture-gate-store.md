# Fixture gate-store source work

This is an unhooked, work-in-progress implementation of the isolated storage proposal accepted for source development in thread31 post1616. Document acceptance is not source approval or permission to deploy, provision privileged state, integrate callers, or enable the pilot.

The concrete contract is artifact016798f0-b664-4f79-adc6-36a0f2d2bcd1, SHA256 `bb0814903797e910e3c825d4377ada549d45d368a214ea2b1a5f1a077ca50ad9`.

## Record boundary

`agent_board.night.fixture_gate_dto` provides frozen record/context/selector types and strict canonical JSON encoding/decoding. Version1/policy1 accepts only `FIXTURE_ONLY_V1` origin. It rejects unknown/missing/duplicate fields, bool-as-int, noncanonical IDs/hashes, over-cap or malformed bodies, context/owner mismatches, invalid terminal combinations, and weakened bootstrap/runtime selectors.

The success table permits idempotent false/false changed flags. Domain refusal with committed expiry effects is distinct from successful target latching. Local stop-requested state is not physical stop or settlement authority, and the record may retain native uncertainty.

`TrustedAssertions` contains caller assertions. Neither decoding nor constructing it acquires pidfds, observes exit, authenticates the caller, validates executable integrity, drains a result channel or proves handle containment. The release hash and fence digest are correlation data, not authentication. A structurally valid record cannot make false assertions true. Fixture bootstrap is explicitly simulated setup, not privileged provisioning authorization. A reference is a deterministic identifier, not a capability.

No module is imported by a runtime supervisor, journal/ledger writer, service or production caller. The codec itself performs no filesystem, SQLite, process or network operations.

## Internal SQLite core

`agent_board.night.fixture_gate_sql` defines the exact three-table schema, four-statement schema/header validation and four-query current bundle. Catalog enumeration stops at nine objects; control enumeration stops at two rows; identity/completion queries use fixed unique indexes and capped projections. Both current-operation and current-launch paths must agree. Snapshot checks reject autocommit use.

These are private connection-level helpers. `_current` returns transaction-local data, not a completed public read result: it does not establish successful COMMIT, confirmed resource closure, private-file provenance or an externally usable observation. A real existing connection is not an authority or protected provisioning mechanism.

Both operation and current-launch completion paths must be absent for PENDING/FAULTED or identify the same complete record for BOOTSTRAP/COMPLETED, including retained history in FAULT_LOCKED. Relevant orphan conflicts refuse before DML. Unreachable unrelated history is not globally audited by bounded APIs.

## Explicit file API

`agent_board.night.fixture_gate_store` provides `observe`, `provision_fixture`, `begin_operation`, `complete_operation` and `lock_fault`. Context and operation arguments are keyword-only. Each invocation opens a fresh connection without accepting one from the caller. Runtime opens use existing-file `mode=ro` or `mode=rw`, with zero SQLite timeout and zero busy timeout. Reads expose an observation only after validated coherent snapshot, successful COMMIT and confirmed close.

Real private-parent/file checks and path inode comparisons surround connection open. A bounded, no-follow header probe rejects non-delete SQLite header modes and empty/corrupt headers before SQLite opens them. Retained journal/WAL/shared-memory paths refuse rather than being recovered, deleted or adopted by runtime APIs. This conservative preflight may refuse during another writer's journal lifetime; it is not a promise of uninterrupted read availability. Concurrent file/schema/sidecar replacement, restore, provisioning and uncooperating writers remain excluded by the caller. Path checks do not establish hostile same-UID/root race protection or atomic VFS-open authority.

Fixture provisioning alone creates an exclusive0600 file in an existing private parent, fsyncs the parent before SQLite setup, creates the pinned schema and explicit fixture bootstrap in one transaction, and fsyncs the parent after SQLite completion and close. Any existing path refuses. Failed artifacts are retained; there is no implicit retry, repair, adoption, reset, migration or deletion. A retained sidecar without a main file also refuses, without creating the main file.

Begin requires validated IDLE, checks retained identities and completions by both new keys before DML, then atomically publishes the new PENDING identity/header. Complete requires the entire current pending owner and a strict matching attestation, atomically inserting completion, changing identity status and publishing the IDLE reference. Fault requires an explicit matching selector/state and a fixed reason. Bootstrap/runtime selectors are not interchangeable. Fault cannot reopen or clear a gate, and retained completion history is validated even after fault locking.

Mutation, cleanup and provisioning directory publication have independent result axes. COMMIT exceptions remain UNKNOWN even after rollback or a later readable snapshot. Known commit survives close failure. Confirmed close can establish rollback of a transaction that never attempted COMMIT. There is at most one explicit cleanup rollback and no automatic follow-up mutation. A confirmed final directory fsync is not erased by a subsequent directory-descriptor close failure, but uncertain cleanup still prevents provisioning completion. A fresh durable observation does not dispose an old live handle or rewrite an earlier result.

## Executable evidence and remaining work

Tests use real private files and SQLite connections, including real fault-injecting connection subclasses, not permission mocks. Coverage includes semantic terminal combinations and canonical byte caps, selector/state/owner/replay refusals, valid-index current-launch and requested-key orphans, atomic completion rollback, COMMIT/rollback/close combinations, directory-sync faults, and retained old handles.

Process-crash cases terminate an independent interpreter before/after each provisioning schema/data stage, each runtime mutation stage, COMMIT, SQLite close and both directory fsyncs. Runtime observation must not repair retained artifacts. Separate, explicitly named fixture-maintenance inspection then checks atomic control/identity/completion state; it is not part of the runtime API or permission to recover production storage.

A synchronized two-thread test uses both public APIs and real independent connections: the reader completes its original coherent snapshot while a writer holds uncommitted changes, and a later fresh read sees the committed new state. No test-only barrier or callback is accepted by the public API.

The measured complete API success SQL counts are14/20/17/16/22 for observe/begin/complete/fault/provision. Failed-COMMIT-before-execute cases with one cleanup rollback measure15/21/18/17/23; an after-COMMIT exception does not invent a rollback when SQLite has already ended the transaction. Runtime success counts and fixed indexed SEARCH plans are also checked with10,10000 and100000 valid retained completion records. Provisioning starts empty, so its22-statement count is measured on a new file. The core V4+B4 subset remains exactly eight statements. These bounds count explicit SQL calls, not VM instructions, storage latency, crash recovery or hard wall time.

This private checkpoint passes314 focused tests and2214 full combined tests (five known fork warnings), Ruff/format and mypy189. The complete fault-stage audit, refreshed gates after any further changes, and independent exact-head source review remain outstanding. No source-review request has been published. This remains private source development, not an approved production or integrated implementation. Privileged provenance, actual associated-exit acquisition, live handle containment, operational eligibility, startup/restore/all-writer audits, manager/native-stop proof and operations remain separate gates. Ticket11 remains owned by pi@dev and the pilot stays disabled.
