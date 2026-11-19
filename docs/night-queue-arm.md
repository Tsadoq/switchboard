# Unhooked arm metadata on the effective queue

Source candidate for the control-aware V4 arm metadata contract in board thread 31, draft 1: artifact `3c489637-d7da-4be1-a1c4-dcef95996134`, SHA-256 `630f0f71f744f7f005c505bc933009bdb03c91fc47aae1bd9e87f116bbda6c60`. Ticket 11 stays with pi@dev; the pilot is off. This module has no caller.

## What it is

`night/queue_arm.py` offers one operation on an exact version 4 ledger, with the caller-held `ExpectedControl` from `night/effective_queue.py`:

- `compare_and_set_queue_arm(path, *, expected, publication)` sets the arm row for one run from an exact expected prior, or its explicit absence, to a desired tuple.

It needs an EMPTY queue. A changed arm advances only `effective_queue.revision`, once, in the same transaction; the journal, generation, selection and counter stay as they are, and no receipt is allocated. An exact match rolls back and keeps the token. A present row is a conflict when the prior was given as absent, even if it equals the desired tuple. The result returns the next control and the stored tuple only after a normal commit or intentional rollback with a clean close.

## What it relies on and does not prove

Everything in `docs/night-effective-queue.md` applies. In addition:

- One trusted owner holds the control and serializes queue and arm writes. The legacy guard cannot write a version 4 ledger, and no cross-process handoff exists.
- The supported order is reservation and an established timer fact, then arm publication while EMPTY, then the first binding. The leaf does not check the timer or the reservation.
- A guard restart or re-arm while a receipt is pending leaves the stored row stale until the owner republishes it. Any later reader must treat disagreement with the live guard as not armed. A matching row is correlation metadata, not proof of current enforcement.
- An arm for a run with no binding row is accepted as metadata. It proves neither that the run exists nor that it was admitted.

## Evidence in this change

- Native schedules, pinned: insert 16, replace 16, exact match 11, conflict 11, ACTIVE 10, control mismatch 10. They are identical with 10, 10000 and 100000 rows of unrelated arms and receipts, and the arm lookup is a key search with constant VM steps.
- Every compare-and-set branch and every field, EMPTY and ACTIVE, exhaustion with an exact match still allowed at the maximum, signed zero stored as positive zero, admission without a binding, and token ownership shared with the ordinary queue operations.
- Strict inputs refuse before any SQL; malformed current rows refuse before the prior is compared.
- A failure before and after every statement of every branch; landed and unlanded COMMIT exceptions with cleanup compounds; every column of every post-write read altered; impossible rowcounts with cleanup compounds; the typed BUSY/LOCKED matrix; interpreter exits around DML, COMMIT and close; real file and descriptor cases.

These are fixture tests. They do not certify SQLite's internal allocation, latency, every corrupt state, or anything about native timers, the guard, the server or operation.
