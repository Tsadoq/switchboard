# Fenced held-wake capture

This is an incomplete source integration for ticket 11. The pilot stays disabled.
It does not implement execution acknowledgement, a native launch producer,
repository selection, provider authentication, morning drain or activation.

`night.capture` is available only through the dedicated signed verifier endpoint.
Ordinary agent credentials cannot call it. The target is the admitted run UUID;
the exact payload keys are `fence`, `journal_id`, `counter`, `receipt_id`, `boot_id`,
`invocation_id` and `cgroup`, matching the persisted verifier binding.

A new receipt requires the current journal counter, a running bound run, current
enrollment/global enablement and a fresh in-window deadline check. Pending human
work refuses capture. The server chooses only this principal's budgeted held wakes
for the run's local night, excluding outstanding ordinary leases and wakes already
captured by any unfinished run. No caller chooses a principal, wake ID, repository,
command, path, batch size or deadline.

Capture, its immutable response receipt and the counter increment are one SQLite
IMMEDIATE transaction. At most 20 wakes are selected in ID order, and the serialized
wake-list snapshot is at most 32768 bytes. Summaries have at most 1024 characters;
`summary_truncated` explicitly reports truncation. Oversized first-item metadata
refuses without advancing the counter. Truncation is not a complete thread read.

The response contains `journal_id`, `counter`, `capture_id`, `run_id`, `principal`,
`deadline` and `wakes`. Each wake has `id`, `reason`, `source_ref`, `summary`,
`summary_truncated`, `thread_id` and `topic_id`. The fence is never returned.

An identical persisted receipt retry returns exactly the same snapshot, even if
wake text or run eligibility subsequently changes. A changed retry is rejected.
**A capture response, including a replay, is data, not launch authority.** The
receiver must separately satisfy the trusted launch/stop/deadline checks. This
HTTP response is not an extension of the local socket's 8192-byte protocol.

Capture does not acknowledge a wake, change its ordinary lease, settle a run,
refund a budget or extend a deadline. Ordinary acknowledgement still cannot consume
held rows. An unfinished or quarantined run keeps its captured rows unavailable;
no timeout releases them. A finished run no longer excludes those still-unacknowledged
rows from a later eligible capture, but this is not a claim that native delivery
happened or that a new run can bypass budget, window or capacity checks.

The verifier SDK persists the capture response and policy-derived assignment
atomically through its durable receipt/counter ledger. Its sealed-memfd transport
helper remains unhooked from launch. There is no controller producer or operator
invocation recipe. Never make an out-of-band call that advances the server counter
behind the ledger. Native transport must expose only assignment data, never the
verifier key or run fence. A separate reviewed execution-acceptance acknowledgement
and completion contract is still required before consuming wakes.

[Capture recovery](night-capture-recovery.md) defines server-side atomic abort
resolution for a pending capture racing stop or revocation. It is not permission
to discard uncertain receipts or bypass physical stop proof. Do not enroll or
enable the pilot on this partial integration.
