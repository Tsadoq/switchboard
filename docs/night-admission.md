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

Held wakes currently remain held until the unfinished fenced autonomous-delivery or
completion-tracked morning-drain path consumes them. There is deliberately no automatic
06:00 flush. Do not enroll a principal until these paths and the local stop consumer
are implemented and independently accepted. The default database has no enrollments.

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
