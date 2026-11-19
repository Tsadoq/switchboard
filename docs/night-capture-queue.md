# Unhooked capture and abort episodes on the effective queue

Source candidate for the capture/abort extension contract in board thread 31, draft 1: artifact `10d07eab-8e70-46aa-879d-ffd43f50d0b3`, SHA-256 `f4173e36e226d3c448e2b002f102e10ba033518a461ff64c18f0ca6dc23db3a1`. It extends the ordinary queue in `night/effective_queue.py`, which is unchanged. Ticket 11 stays with pi@dev; the pilot is off. This module has no caller.

## What it is

`night/capture_queue.py` adds five operations on the same exact version 4 ledger, with the same caller-held `ExpectedControl`:

- `observe_capture_episode(path, *, expected)` reads the selected capture or abort episode, if any.
- `publish_capture(path, *, expected, publication)` publishes one capture with its fixed choices (principal, cutoff, policy) on an EMPTY queue, or matches the identical ACTIVE capture.
- `acknowledge_capture(path, *, expected, claim)` applies a snapshot claim at q+1, derives the assignment from the stored choices and empties the queue.
- `publish_capture_abort(path, *, expected, publication)` replaces the ACTIVE capture with an abort that copies its request, or matches the identical ACTIVE abort.
- `resolve_capture_abort(path, *, expected, claim)` applies an aborted or applied proof at q+1 and empties the queue.

Each returns a `CaptureResult` with independent mutation, cleanup, changed, control, episode, completion, refusal and fault. Completion is one of `CapturedCompletion` (snapshot, assignment), `AbortedCompletion` (proof) or `AppliedAfterAbortCompletion` (snapshot, proof). The applied-after-abort variant has no assignment field; the stored assignment column is still derived and verified. No completion grants launch or settlement authority.

## Rules it enforces

- Packets are compact canonical JSON, new-code-only. Legacy-written capture state, whose receipt or policy uses default separators, refuses as `ROW_REFUSED` without normalization.
- Capture is once per run. After an aborted resolution the ack0 parent and its context stay, and a fresh capture for that run refuses as `CAPTURE_ALREADY_EXISTS`.
- Both abort branches advance the counter once, to q+1, as the server does.
- A late parent acknowledgement with the old token refuses as `CONTROL_MISMATCH`; given the current abort token it is `WRONG_ACTION`.
- A packet that fails its cap, strict decoding or schema refuses as `CAPTURE_UNUSABLE` before SQLite opens. A snapshot that cannot become an assignment refuses as `CAPTURE_UNUSABLE` after BEGIN and rolls back.

## What it relies on and does not prove

Everything in `docs/night-effective-queue.md` applies here too. In addition, the caller must only publish an abort for a run that is stop-requested or latched stopping or quarantined; the leaf does not read or verify that. Proofs and snapshots are request-correlated claims, not authenticated. An aborted resolution does not finish a server run, release a native slot or refund a charge.

## Evidence in this change

- Native schedules, each pinned under its contract ceiling: observe EMPTY 10 (10), observe or identical capture 14 (18), observe or identical abort 15 (20), publish capture 25 (26), acknowledge capture 23 (25), publish abort 28 (29), resolve aborted 26 (27), resolve applied 28 (29). They are identical with 10, 10000 and 100000 rows of history including ack0 parents behind resolved aborts, and every capture lookup uses a key.
- Semantics: both lifecycles with exact rows and derived assignments; identical and conflicting matches; once per run; both wrong-action cases; every correlation refusal; unusable packets before and after BEGIN; three legacy codec cases.
- A failure before and after every statement of all ten transitions; landed and unlanded COMMIT exceptions compounded with rollback and close faults; a real reader-blocked COMMIT.
- Valid-but-wrong writes during each mutation, impossible rowcounts, row ids and post-write reads with cleanup compounds, and the typed BUSY/LOCKED matrix, all ending in rollback with no completion.
- Interpreter exits around the first and last DML, COMMIT and close; a retained journal refuses with zero SQL and unchanged bytes.
- Real file and descriptor cases.
- Against the real board server with signed requests: the aborted branch tombstones a delayed parent request; the applied branch with a lost reply returns history only, and the cached snapshot cannot acknowledge anything afterwards; a server-applied capture whose local resolution COMMIT fails is UNKNOWN with no completion.

These are fixture tests. They do not certify SQLite's internal allocation, latency, every corrupt state, or anything about native execution, the server or operation.
