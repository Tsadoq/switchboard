# Unhooked live capture association

Source candidate for the named live capture association contract in board thread 31, draft 1: artifact `ad952d85-f8db-4f21-8a0f-31c1c551b343`, SHA-256 `02f8a1a34397b7296ebf55118cfb03c02d19d9b32b28d112d48b0cd3c356dd6a`. Ticket 11 stays with pi@dev; the pilot is off. This module has no caller.

## What it is

`night/queue_association.py` offers one read-only operation:

- `read_capture_association(journal_path, ledger_path, *, expected, control, completion, guard, now, grace)` checks a coordinator's retained normal `CapturedCompletion`, its current EMPTY `ExpectedControl` and an independently obtained guard tuple against LocalJournal version 2 and the version 4 ledger, and returns plain `CaptureAssociation` metadata.

It opens each file read-only in its own deferred read transaction, reads only named rows by key, and always rolls both back. It never writes, retries, reconciles or rebuilds a completion or control from disk. Its success schedule is 25 statements across both files.

## What it relies on and does not prove

Everything in `docs/night-effective-queue.md` and `docs/night-capture-queue.md` applies. In addition:

- The coordinator that owns the control runs it between queue writes and holds exclusion over every journal and ledger writer, the guard and root latch included, until both handles close. A reader holding a read lock blocks a journal stop or a queue COMMIT; without that exclusion, a read can delay a stop or turn the owner's COMMIT into UNKNOWN.
- Two read transactions are not a paired snapshot, and nothing closes the race after the last check.
- Both files must be owned by the coordinator's effective uid with mode exactly 0600. Path checks after connect do not pin SQLite's descriptors.
- A matching arm row and guard tuple are correlation metadata, not proof of current enforcement.
- The result is not a launch permit, a freshness witness or settlement evidence, and an empty assignment does not authorize launching empty work.
- Real writer contention surfaces after a successful deferred BEGIN, so it is reported as `SQLITE_FAILURE`, not `BUSY`.
- The capture receipt's sequence number is range-checked only; no retained input names it.

## Evidence in this change

- The 25-statement success schedule, pinned, and identical with 10, 10000 and 100000 rows of history in both files, including ack0 parents behind resolved aborts. Journal lookups use an index with constant VM steps on hit and miss.
- Every refusal class: invalid input, unusable completion, path, sidecar, schema, identity, control, active queue, rows, journal eligibility, association mismatch, abort link and expiry; exact mode 0600 for both files.
- A failure before and after every statement; rollback and close faults on either file with per-file cleanup and close-first precedence; the typed BUSY/LOCKED stages; a real held lock.
- Every column of every read altered and every read given the wrong cardinality; deleted input fields; inputs changed during the open; no owned descriptor or connection left after any outcome.
- With exclusion deliberately absent, an open reader makes a journal stop COMMIT fail with BUSY, as the contract warns.

These are fixture tests. They do not certify SQLite's internal allocation, latency, every corrupt state, or anything about native execution, the guard, the server or operation.
