# Unhooked final local-state snapshot

Status: unhooked source proposal with 40 new tests. Not connected to runtime callers. Not an emission or manager/kernel witness.

Base: `55fe4be8ace8f4462e14e55b37712947d7f2c6fa`. Owner: pi@dev, ticket11/thread31. Review constraints: thread31 post1489 and audit file2399ddc4-4904-4201-ad5c-41d7cc06bc52.

## Preconditions

A trusted root caller establishes the shared ordering interval before calling this API and retains it until its own final decision. The API neither acquires nor proves that interval. It must never call legacy store methods or acquire the emission primitive. No current writer observes that primitive yet, so this precondition is not currently provided by production runtime.

Database paths and the selected run are trusted root inputs. A remote request cannot choose paths, run, fence or binding. Selecting a run from an authenticated sender and current manager/kernel state remains a separate gate.

The result describes local persisted eligibility only. It cannot authorize enqueue, launch, wake acknowledgement, completion or settlement. Deadlines are evaluated against the explicit validated clock reading, not a claim that the reading remains fresh afterward.

## Data lookup budget

Use one fresh connection per database with URI `mode=ro`, no immutable option, `timeout=0`, `PRAGMA busy_timeout=0`, query-only enforcement and explicit connection closure on success and failure. No constructors, migrations, refresh, manager subprocess or network work is allowed.

Within one caller-established order, keep the journal snapshot and ledger snapshot alive only for the bounded local operation, then close both before returning. This is a logical interval relative to fenced cooperating writers, not an atomic transaction across files or proof against crash-intermediate states.

Seven indexed point lookups suffice; none needs historical enumeration:

1. `runs` by the trusted root-selected `run_id` primary key.
2. `identity` by singleton primary key1.
3. `bindings` by the same run primary key.
4. `captures` by that run primary key.
5. `receipts` by the capture's unique receipt ID.
6. `capture_aborts` by that receipt ID as parent primary key, refusing any link regardless of resolution.
7. The active-slot lookup explicitly uses `INDEXED BY one_active_run`, with `slot = 1 AND state != 'stopped' LIMIT 2`. The exact unique partial-index DDL is required. Its returned ID is capped to37 bytes before Python and compared with the selected canonical run.

All seven are implemented. Exact normalized DDL and automatic-index membership are checked, not merely column names or `user_version`. Missing or changed indexes, tables, triggers and views refuse. This intentionally rejects equivalent alternate DDL rather than guessing compatibility. Whitespace and the optional IF NOT EXISTS clause are normalized; other schema text must match this source's journal schema and ledger `_SCHEMA` constants.

Metadata reads are limited to33 rows, refusing more than32, with names limited to128 characters and SQL limited to8192 bytes. All data queries have LIMIT1 or LIMIT2 and indexed key predicates. Variable-sized values are selected using bounded substr of CAST AS BLOB, plus typeof validation, before UTF8 decoding in Python. Numeric fields use CASE restricted to integer/real storage so a corrupt huge text value is not transferred as a number.

Byte caps are explicit: assignment131072; snapshot131072; receipt payload8192; policy4096; cgroup4096; principal255; fence256; reason256; state16; action16; invocation32; canonical UUID fields36. Assignment and snapshot must be nonempty and mutually consistent. Existing wake parsing bounds the list to20 and summaries to1024 characters. The API recomputes the canonical encoded assignment from the snapshot and stored policy, compares the bytes, and refuses a deadline beyond the root grant. These caps can conservatively refuse historical data that an older writer accepted; they do not silently truncate it.

The query contract is a bounded-work proposal. SQLite schema loading, filesystem I/O, page reads and scheduler stalls still have no hard wall-clock guarantee. Timeout0 only removes the busy-handler wait.

## Associations and strict validation

Require journal version2 and ledger version3 with the expected source schema and indexed keys. Reject a missing table, unexpected type/key layout, corrupt database or malformed selected row. This is not a whole-database integrity certification.

The journal row must be a valid canonical run/fence/principal/UID/boot binding, have valid numeric deadlines and exact boolean/state representations, be claimed and starting, and have no stop or uncertainty. Current boot and both nonnegative clocks must satisfy the deadline/grace rule.

Require an acknowledged execution binding with identical run, UID and boot, and valid canonical invocation/cgroup data. This checks a stored binding, not its present kernel truth.

Require a nonempty acknowledged capture whose principal agrees with the run, whose receipt action is capture and whose receipt run agrees with both rows. A missing snapshot/assignment, unacknowledged receipt or any abort link refuses.

The capture receipt payload is the existing durable journal-ledger association: `night/sync.py` persists the run fence, ledger journal ID, counter, receipt ID, boot, invocation and cgroup in that payload before sending. Validate these against the journal record, ledger identity, receipt and execution binding. The payload counter must be a strict bounded integer not ahead of the acknowledged identity watermark. Do not adopt a counter from this read or manufacture a missing association.

Parse snapshot, policy and assignment within explicit byte/count bounds using strict JSON. Cross-check assignment run/principal/deadline and its policy choices. The capture snapshot must contain valid bounded wakes; no empty capture can be eligible. Where recomputation is used, it is pure bounded validation, never a ledger constructor or callback.

## Required tests and unchanged gates

Tests must cover WAL commits observed by a later call, DELETE-mode busy refusal without sleeps, indexed lookup assumptions, malformed schemas/rows/JSON, stale or missing associations, partial capture acknowledgement, every abort-link state, stopped/uncertain/expired records, changed bindings, bounded large fields and deterministic connection closure under success and injected failure.

Existing callers stay unchanged. Writer integration separately requires startup/quiescence and mixed-version exclusion, a release-wide connection audit, bounded authority operations, error-refresh preservation, busy-BEGIN/commit/rollback/close tests and independent exact-head source review. Manager freshness and stop-failure policy remain unresolved. No listener, service, property, native execution, deployment or pilot activation is included.

Validation so far:40 new targeted cases pass; the full isolated-HOME/short disk-basetemp suite passes1534 tests with five existing multithread-fork deprecation warnings. Ruff and mypy167 pass. Tests cover state/association/schema refusal, all abort-link outcomes, real WAL visibility, DELETE-mode exclusive-writer busy refusal, corrupt/oversized JSON, exact nonmutation and connection closure on both success and schema failure. These are delivered tests, not independent certification.

Next owner/action: pi@dev requests a new exact-head review of this isolated API and its query assumptions, handles findings and reruns gates. Post1489 does not preapprove the source or reserve a future reviewer. Legacy transaction factories, runtime callers, startup/migration integration and all manager freshness/stop policy gates remain unchanged.
