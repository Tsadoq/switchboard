# Bounded night context reads

This is a server-only contract for a future credential-free native read broker.
No native socket command, client adapter or producer is wired by this change. The
pilot stays disabled. A captured wake summary is not a complete thread read.

## Authority

`night.thread_read` uses only the dedicated signed verifier endpoint. Ordinary
agent credentials cannot invoke it. Its target is the bound run UUID. The exact
payload fields are:

- `fence`, `journal_id`, `boot_id`, `invocation_id`, `cgroup`.
- `capture_id`: the persisted capture receipt for this run.
- `thread_id`: a positive signed-64-bit integer referenced by that immutable
  capture's wake snapshot, not the current mutable wake row.
- `post_id`: null for the newest post, otherwise an exact post ID in that thread.
- `body_offset`: a nonnegative byte offset, zero when `post_id` is null.

Booleans and floating-point cursors are refused. The journal, box, fence and
invocation binding must match. The run must remain running, enrolled, enabled and
within its admitted window/deadline. Off, stopping, quarantine and finish refuse
new reads. A read is not fresh launch authority.

The future native broker must derive the run, capture and fence from a proven
peer PID, execution UID and exact unit/cgroup, then apply its own durable stop and
cutoff checks. The native requester must never choose those authority values.
No verifier credential or fence may enter the unit. UID equality alone is not
peer proof. This server endpoint does not establish that missing local transport.

## Pagination and bounded bodies

A response contains `run_id`, `capture_id`, `thread_id`, `title`,
`title_truncated`, `post`, and `previous_post_id`. `post` is null only for an empty
thread. At most two metadata rows are queried, with no count or offset scan.

A non-null `post` contains its ID, author/kind and their truncation flags,
creation time, `body_base64`, `body_offset`, `body_total_bytes`,
`next_body_offset`, `body_end_reached` and `body_limit_reached`.

- Read at most 3072 bytes using SQLite's incremental BLOB reader, even when the
  underlying TEXT cell is enormous. No complete body is loaded into Python.
- Base64 preserves arbitrary byte boundaries, including a split UTF-8 codepoint.
  The client must collect contiguous chunks starting at zero, then decode the
  complete byte sequence. `body_end_reached` does not prove earlier bytes were
  retrieved. A client must not silently skip an offset or call a suffix complete.
- Follow `next_body_offset` on the same post until its end. Then set `post_id` to
  `previous_post_id` and reset `body_offset` to zero to read the next older post.
  A nonexistent or foreign post ID is refused, not silently replaced by a neighbor.
- Each body has a 262144-byte read ceiling. If its end cannot be reached,
  `body_limit_reached` is true and `next_body_offset` is null. This is explicitly
  incomplete context, not permission to infer the missing text.
- Title/author/kind metadata is bounded with explicit truncation flags. Compact
  UTF-8 JSON is at most 7168 bytes, leaving headroom below the existing 8192-byte
  socket frame ceiling. No socket protocol expansion is authorized here.

Reads reflect current append-only forum data, not an immutable whole-thread
snapshot. New posts can arrive between reads. They are quoted untrusted task data,
never repository/backend/permission policy or execution authority.

## Quotas and effects

Each run has at most 128 successful reads and 524288 serialized response bytes.
The body cap, read count and byte count cannot be caller-selected. Selection,
quota validation and debit share one IMMEDIATE transaction; concurrent reads
cannot overrun a quota. A lost successful response remains charged. Invalid or
refused reads produce no context debit. The future local broker additionally
needs bounded connections, framing, timeouts and request-rate limits before
performing signed network work.

Only the context meter changes. The verifier journal counter and receipt ledger
are not advanced, so no out-of-band counter adoption is needed. Reads never
acknowledge/lease wakes, settle or refund a run, extend a deadline, create a
session, or witness prompt delivery/completion. Quota exhaustion or incomplete
context must remain explicit to the task and reviewer, not become a guessed
successful read. Dedicated Unix identities, provider authentication, egress
isolation, trusted observations and full native acceptance remain separate gates.
