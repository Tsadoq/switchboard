# Durable capture-abort resolution

This is a server-only recovery contract. It does not hook capture or native launch
into the controller, acknowledge wakes, prove execution acceptance or authorize
installation, enrollment or activation. Do not call it outside the durable client
mutation ledger.

A capture can be denied after admission when global off, expiry or human preemption
wins the race. Retaining its pending receipt forever would prevent later stop
evidence. Conversely, an absent-receipt read is not enough to discard it: the
original request could still arrive after that read. Resolution therefore requires
an atomic tombstone or the exact already-applied capture response.

## Request

The dedicated signed verifier endpoint accepts `night.capture_abort`, targeting the
same run UUID as the pending capture. Ordinary agent credentials are refused.

Copy the original persisted capture payload, replace `receipt_id` with a new,
durably persisted abort receipt UUID, and add:

- `capture_receipt_id`: the original capture receipt UUID.
- `capture_counter`: the original capture request's counter.

The exact nine fields are `fence`, `journal_id`, `counter`, `receipt_id`, `boot_id`,
`invocation_id`, `cgroup`, `capture_receipt_id` and `capture_counter`.
`counter` must equal `capture_counter`, and both are nonnegative integers, not
booleans. The two receipt IDs must differ. The binding is copied from the parent,
not re-resolved from mutable configuration.

The server reconstructs the entire original seven-field request and computes its
canonical action/target/payload digest. An extra caller-asserted digest is neither
required nor accepted. The journal, fence, box, agent role and persisted invocation
binding must all match. Recovery remains available after off, unenrollment, expiry,
stopping, quarantine and finish; it does not call the admission/start operation.

## Response

Both outcomes have exactly these fields:

- `journal_id`, `run_id`: the original bound journal and run.
- `abort_id`: this abort's own receipt UUID.
- `capture_id`, `capture_counter`: the original capture receipt and request counter.
- `counter`: `capture_counter + 1`.
- `outcome`: `aborted` or `applied`.
- `capture`: null for `aborted`; the exact original capture response for `applied`.

### Aborted

The original receipt has not been applied, and the current server counter must
still equal the parent request counter. One IMMEDIATE transaction saves the abort
response, advances the counter once, and inserts a permanent tombstone binding the
original receipt ID to its exact original request digest.

Every later attempt to apply that original capture is refused as `capture_aborted`,
even if delayed in transit. Changing the request, or reusing its receipt ID for a
different action, is `receipt_conflict`. A failure writing any part rolls back the
receipt, counter and tombstone together.

The client must atomically save this proof, resolve the parent as not applied, and
acknowledge the abort at the returned counter. It must not synthesize a parent
capture acknowledgement or derive an assignment.

### Applied

The original receipt was already applied with the exact reconstructed request.
Its stored response and the current high-watermark must both have counter
`capture_counter + 1`. Unrelated counter divergence is refused. The response
includes that original immutable snapshot.

Saving this resolution receipt does **not** advance the counter a second time and
does not install a tombstone over the existing capture. The client must validate
the nested snapshot against the persisted parent using the normal capture checks,
then atomically acknowledge that parent, store its derived assignment, and resolve
the abort as superseded. This is a resolution receipt, not a second applied counter
mutation. A stopped run must not launch the stored assignment.

## Replay and sequencing

The abort receipt and its parent link must be durable before sending. Once that
link exists, reconciliation sends only the identical abort, never the parent again.
An identical abort replay returns its exact saved response, even after subsequent
stop evidence advances the counter. A changed abort receipt is refused. A new
receipt is not a substitute for the persisted abort receipt.

Only after resolving both ledger entries atomically may the client compose stop
evidence at the next counter. Malformed or contradictory proof remains pending and
frozen. Physical stop proof is still separately required before capacity release.

Neither outcome acknowledges or leases wakes, settles the run, refunds budget,
extends the deadline or demonstrates prompt delivery. An applied snapshot remains
data, not fresh launch authority. Completed/empty cgroup evidence remains distinct
from the future execution-acceptance and turn-completion witnesses.
