# Night admission foundation

This is the accounting and scheduling foundation for ticket 11, not an enabled nightly
pilot. It has no HTTP/signed-action routes, runner integration, periodic task or public
controls. Migration 0015 creates a disabled control row and no enrollments. Ordinary
wake leasing and delivery remain unchanged. Applying this code cannot start sessions.

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
  Later verified termination can release that capacity but cannot refund the charge.
- Turning the control off or unenrolling requests stop without freeing capacity.

## Trust boundary and unfinished integration

`settle(..., stopped=True, elapsed=...)` is an internal trusted-controller interface,
not an agent assertion or a termination verifier. No external route exposes it. Its
inputs must eventually come from verified local containment and monotonic runtime
measurement. The accounting module alone cannot prove a process stopped, enforce an
OS runtime deadline or contain remote effects. Do not enable the pilot by exposing
this method to an ordinary runner or accepting a boolean in a signed agent payload.

Remaining work before enrollment/enablement:

1. Per-run transient user units/cgroups, dedicated daemon home and isolated worktree;
   budgeted termination grace, local kill/empty-cgroup proof and restart recovery.
   No shared daemon kills and no release of uncertain capacity. Test detached and
   SIGTERM-resistant children, runner loss and dedicated/ordinary daemon isolation.
2. Trusted human/incident classification before leasing. Human web authorship and an
   allowlisted signed principal in a designated incident room are separate paths.
   Split mixed batches, stop/preempt autonomy and verify termination before ordinary
   potentially conflicting work. Do not classify incidents by text keywords.
3. Integrate budgeted initial/timer/follow-on delivery, busy-session batching and stale
   launch rejection. Filter held work in SQL leasing, not lease/refuse churn.
4. Completion-tracked per-host paced morning backlog drain and deduplicated 07:30 digest.
5. Disabled-by-default authenticated UI controls, repository-owner policy verification
   (personal owner Tsadoq only, excluded organizations/projects), operator limits and
   independent review. No deployments, credentials or permission/security changes.
6. Managed dry-run/apply records and dedicated-home device approval, then an explicitly
   enrolled small pilot with a tested off/recovery path. Existing ordinary timers are
   not evidence that any of these runtime guarantees have been delivered.

Ops verified user-manager CPU/memory/pids delegation on candidate boxes. The proposed
systemd RuntimeMaxSec must fit inside the reserved budget and window; stop grace cannot
be added beyond them. Dedicated daemon homes sharing an account require a separate
interference test before enabling. Local cgroups do not contain ssh, pushes or API calls.
