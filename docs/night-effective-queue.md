# Unhooked effective queue for ordinary binding and evidence

Source candidate for the first-slice contract in board thread 31, draft 1: artifact `8c8b6bb4-5c22-4ba7-af11-c127170c568a`, SHA-256 `c95b97d5ffefec030fcf8e0335d227a13698cb0d8aaa4548df4adf34a1bf1a08`. Ticket 11 stays with pi@dev; the pilot is off. This module has no caller.

## What it is

`night/effective_queue.py` offers three operations on an exact version 4 ledger, which is the sixteen V3 objects plus `effective_queue`:

- `observe_ordinary(path, *, expected)` reads the selected ordinary receipt, if any, and changes nothing.
- `publish_ordinary(path, *, expected, publication)` publishes one `BindingPublication` or `EvidencePublication` on an EMPTY queue, or matches the identical ACTIVE one and changes nothing.
- `acknowledge_ordinary(path, *, expected, ack)` applies one request-correlated `OrdinaryAckClaim` to the selected receipt and empties the queue.

Each returns a `QueueResult` with independent mutation, cleanup, changed, control, selected, refusal and fault. `control` and `selected` are returned only after a normal COMMIT or an intentional unchanged rollback with a clean close. Every COMMIT exception is UNKNOWN and returns neither.

## What it relies on and does not prove

- `ExpectedControl` lives only in the trusted caller's memory. Fixture construction gives the first value; each usable result gives the next. There is no persisted store or restart loader, so losing the caller ends the lifecycle. Any later persisted design needs a store that cannot be restored together with the queue file; the leaf cannot check that.
- After a COMMIT exception the caller must stop using the lifecycle. The leaf withholds the next control; it cannot prove the caller obeyed.
- A rollback to an older revision refuses against a retained newer control. An identical restored file presented with an equally restored control is indistinguishable and outside the guarantee.
- `generation` detects a control used against a different database. It is not lineage, freshness or anti-rollback.
- The acknowledgement is a claim. The leaf does not authenticate the server or the request correlation.
- EMPTY means the validated control selects nothing under the trusted all-writer invariant. It is not an audit of history, of requests in flight, or of native or server state.
- Path and inode checks do not pin SQLite's descriptor or resist a hostile actor with the same UID or root.

## Not included

Runtime creation, DDL, migration, bootstrap, capture, capture_abort, restore, cold admission, recovery reading, candidate controls, retries and any caller integration. Bootstrap, capture and capture_abort selections refuse as `UNSUPPORTED_ACTION`. The legacy `Ledger` refuses version 4 unchanged.

## Evidence in this change

- Lifecycle and semantics: the binding, ack, evidence, ack cycle with exact tables; identical ACTIVE rollback; every conflict and refusal in the contract; stale revision, generation, counter or selection; the evidence outcome and state table; a stale ack against a later episode; rollback to older file bytes.
- Inputs: exact types, subclasses and `object.__setattr__` tampering refuse as `INVALID_INPUT` before any SQL.
- Persisted state: malformed, oversized, non-canonical, duplicate-key and NaN payloads; wrong counter, journal, receipt or binding; unsupported actions; a suppressed parent; inconsistent and exhausted sequence metadata; exhausted revision and counter; any extra, missing or mutated catalog object or other version.
- Native statement counts per transition, all within the 32 ceiling: observe 10 EMPTY and 13 ACTIVE, publish binding 22, identical 13, publish evidence 22, ack binding 21, ack evidence 20, stale control 10. They are identical with 10, 10000 and 100000 rows of unrelated history, including hidden-parent capture history, and every lookup uses a key. The greatest-sequence query is labelled SCAN but is a reverse rowid walk with constant SQLite VM steps.
- A failure before and after every statement of every transition returns no control, keeps at most one rollback and reports COMMIT as UNKNOWN whether or not it landed. COMMIT failures compounded with rollback and close faults keep cleanup-first precedence. A held write lock is BUSY before BEGIN; a reader blocking the real COMMIT leaves it UNKNOWN.
- Interpreter exits around the first and last DML, COMMIT and close return nothing. A retained journal refuses with zero SQL and unchanged bytes; later fixture maintenance shows the write landed exactly when COMMIT completed.
- Real file cases: missing, symlink, hardlink, directory, FIFO, bad header, WAL header, permissions, ancestry, sidecars of each kind, inode replacement around open and probe descriptor faults.

These are fixture tests. They do not certify SQLite's internal allocation, latency, every corrupt state, or anything about native execution, the server or operation.
