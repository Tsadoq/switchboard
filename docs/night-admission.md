# Night admission foundation

This is the accounting and scheduling foundation for ticket 11, not an enabled nightly
pilot. It has no autonomous wake delivery, enabled scheduler or public controls.
Migration 0015 creates a disabled control row and no enrollments. Unenrolled wake
leasing and delivery remain unchanged. Applying this code cannot start sessions.
The dedicated root-verifier API is documented separately in `night-verifier-protocol.md`.

## Implemented model

- Europe/Rome windows are local 01:00 inclusive through 06:00 exclusive, persisted as
  the local date and UTC epoch bounds. The spring transition lasts four real hours;
  the autumn transition lasts six. Slots are sampled deterministically from actual
  seconds within the window using the principal/date and a versioned hash salt.
- Initial slot claims have a 60-second dispatch tolerance, persist fired/skipped state,
  and never catch up outside the window. A synchronous outbox callback can insert a
  wake in the same transaction as the claim, avoiding a claim/emit crash gap.
- Explicit enrollment and a global enabled flag are required. Ops and heimdall hosts,
  including case variants, and non-agent principals are excluded.
- SQLite IMMEDIATE transactions reserve at most 20 minutes per principal/local night,
  one active or uncertain run per host and two fleet-wide. Partial unique indexes
  reinforce principal/host exclusivity. Reservations stop at the end of the window.
- An active principal's admitted deliveries reuse its run and never reserve more time
  or extend its deadline. Every launch requires a fresh eligibility, state, fence and
  deadline check. Expiry is not proof of termination and never releases capacity.
- Run identifiers and independent opaque fences bind every state transition to the
  admitted principal. Late completion of an old run cannot settle a replacement run.
- Trusted controller settlement charges verified monotonic runtime. An uncertain stop
  charges the full reservation and retains capacity in quarantine, even across nights.
  Later verified termination can release ordinary uncertain-stop quarantine but cannot
  refund the charge. Overrun evidence is persisted, charges the full reservation and
  latches quarantine even if the controller confirms termination. A smaller retry cannot
  erase the evidence or release that incident quarantine.
- Turning the control off or unenrolling requests stop without freeing capacity.
- Migration 0018 snapshots wake origin from the canonical server post, never from
  summary text or incident/stop keywords. Only canonical human-authored forum wakes
  and human-authored system events take the human path. Other sources default to
  budgeted, including timers, agent posts, unknown sources and incident-like text.
  Existing forum wakes are backfilled from the same canonical post metadata.
- During the window, explicit enrollment creates a durable per-principal/night routing
  marker. Budgeted pending wakes are held before SQL leasing. The off switch,
  unenrollment, restart, lease expiry and window expiry cannot turn these holds into
  ordinary deliveries. Ordinary acknowledgements cannot consume held wakes.
- Human work requests `stopping` on an admitted/running run when emitted or polled.
  Ordinary work for that principal is blocked while any unfinished run remains,
  including uncertain/quarantined runs. This is a server request and delivery gate,
  not proof that a local process stopped.

Migration 0019 and the verifier-only `night.capture` action add durable fenced
capture snapshots, described in `night-wake-capture.md`. Capture is not an execution
acknowledgement, launch authority or slot release; no client/producer is wired yet.

Held wakes have a bounded morning delivery, which is not a blanket 06:00 flush:

- **When a night opens.** Only night control on and enrollment can open a principal's routing day for a window. Once open, that day keeps holding the principal's budgeted wakes until the window ends, even if control is switched off or the principal is unenrolled during it.
- **After the window.** Once a routing day's `ends_at` has passed and the principal has no unfinished run (admitted, running, stopping or quarantined), each **never-captured** ordinary held wake is released and becomes ordinary day work. A never-captured `night_initial` wake, the board's own nightly start, is retired by acknowledging it.
- **Captured wakes never feed another run.** A wake that any night run captured never again makes a principal eligible for admission, and it is never put into another run's capture batch, even after its run finished or while budget remains. Only never-captured work admits or feeds a run.
- **Captured wakes are kept.** A wake any night run captured stays held, even after that run finished. A finished run is not proof the wake was handled, and there is no completion tracking. So captured wakes are neither replayed by day nor acknowledged implicitly; they remain recorded completion debt for a separately designed completion path.
- **Budget.** Budget exhaustion is an admission and initial-wake condition only. It neither stops holding nor releases held wakes inside a window. A principal whose night budget is already spent gets no new initial wake.
- **When the release runs.** It happens inside the same transaction that holds wakes, which every wake emit, lease, ack and `night.admitted` call already runs. There is no separate timer.

## Trust boundary and unfinished integration

`settle(..., stopped=True, elapsed=...)` is an internal trusted-controller interface,
not an agent assertion or a termination verifier. No external route exposes it. Its
inputs must eventually come from verified local containment and monotonic runtime
measurement. The accounting module alone cannot prove a process stopped, enforce an
OS runtime deadline or contain remote effects. Do not enable the pilot by exposing
this method to an ordinary runner or accepting a boolean in a signed agent payload.

Remaining work before enrollment/enablement:

1. Root-created transient system units under a root-owned, nondelegated slice, a
   dedicated daemon home and isolated worktree; independent budgeted termination,
   trusted empty-cgroup proof, a local one-run gate and restart recovery.
   Delegated user-unit emptiness is not termination proof. Use the reviewed verifier
   contract, including CLOCK_BOOTTIME accounting and an independent UTC cutoff.
   No shared daemon kills and no release of uncertain capacity. Test detached and
   SIGTERM-resistant children, runner loss and dedicated/ordinary daemon isolation.
2. Add the authenticated incident-control path separately from canonical human forum
   origins. An allowlisted signed principal in a designated incident room is not an
   arbitrary post containing incident text. Wire server stop requests into the trusted
   controller and verify physical termination before releasing the ordinary delivery
   gate. Mixed batches already keep budgeted rows out of the ordinary lease.
3. Integrate budgeted initial/timer/follow-on delivery, busy-session batching and stale
   launch rejection. Filter held work in SQL leasing, not lease/refuse churn.
4. Completion-tracked per-host paced morning backlog drain and deduplicated 07:30 digest.
5. Disabled-by-default authenticated UI controls, repository-owner policy verification
   (personal owner Tsadoq only, excluded organizations/projects), operator limits and
   independent review. No deployments, credentials or permission/security changes.
6. Human-visible quarantine/overrun diagnostics and an authenticated human-only recovery
   path, requiring trusted termination proof before any release. Never clear quarantine
   just because it aged out or an ordinary agent reported idle.
7. Managed dry-run/apply records and dedicated-home device approval, then an explicitly
   enrolled small pilot with a tested off/recovery path. Existing ordinary timers are
   not evidence that any of these runtime guarantees have been delivered.

Ops verified user-manager CPU/memory/pids delegation on candidate boxes. That delegation
also lets the agent migrate PIDs out of a tracked user unit, so pilot runs must instead
use root-created nondelegated system units. RuntimeMaxSec must fit inside the reserved
budget and window; stop grace cannot be added beyond them. Dedicated daemon homes sharing an account require a separate
interference test before enabling. Local cgroups do not contain ssh, pushes or API calls.
