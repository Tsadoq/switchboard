# Root verifier local intent journal

`agent_board.runner.night_state` implements the durable local one-run gate from the
reviewed verifier contract. It is a library, not an installed root service. Nothing
connects it to runner dispatch, a socket, server grants or systemd execution yet.
The pilot remains disabled.

## Inputs and trust boundary

A trusted controller supplies a `Grant` only after authenticating the server and
checking the run/fence, eligibility and configured box/account binding. Constructing
this dataclass does not authenticate a grant. Socket frames contain none of its
principal, UID, boot or deadline fields and must never be promoted directly into it.
The configured principal-to-UID map is operator input, not request input.

The eventual service must enforce root execution and a fixed root-owned state path,
validate its configuration and reconcile existing root-slice work before admission.
The journal walks every ancestor before creation and every connection: directories
must be owned by root or the current effective UID, without group/other write or
symlinks. In the root service that means root ownership throughout, never an agent
home or agent-owned ancestor. The direct state directory remains private and owned
by the effective UID. Tests use ordinary-user private temporary directories and
simulate only the protected ancestor boundary outside each pytest fixture.
The library does not create a root signer, execute a command or confer permissions.

## Implemented invariants

- Private current-effective-UID directory/file ownership, 0700 parent, 0600 database,
  no database symlink or hardlink alias. Unsafe permissions are refused, not repaired.
- SQLite IMMEDIATE transactions plus a partial unique index admit only one nonterminal
  intent per box, independently of the number of grants the server returns.
- Pinned DELETE journal mode and synchronous EXTRA protect the launch claim before
  external side effects. Other journal modes are refused, not silently converted.
  New directory entries and the database parent are fsynced before use.
  The controller must never issue a create request unless `claim_start()` returned true.
- Same run/fence retries return the original record/deadlines and do not create or claim
  another launch. Different fences, principals, UIDs or boot bindings cannot retarget it.
- A launch claim is persisted once before system-unit creation. Crash or lost manager
  replies after that point retain capacity and cannot be cancelled as never launched.
- Both CLOCK_BOOTTIME and UTC deadlines are checked. Boot change or either expired
  bound quarantines claimed/uncertain work and requests stop, never releases it.
  Positively never-claimed, non-uncertain intents instead become stopped, with explicit
  never_claimed_expired or never_claimed_boot_changed reasons. Insufficient start
  margin likewise closes only a never-claimed intent without executing anything.
- Explicit uncertainty latches independently of launch_claimed, retaining capacity
  even when a controller reports uncertainty before a claim. Stop and sweep cannot
  erase that latch or its original cause. All grant/fence representations hide fences.
- A rejected operation rolls back its own writes, then commits the clock sweep in a
  separate transaction, so capacity/fence/binding errors do not hide known expiry.
- An explicitly cancelled intent that was never launch-claimed can become terminal.
  A concurrent claim/stop transaction cannot mark a claimed launch as never launched.
  The terminal UUID remains recorded and cannot be replayed for another creation.
- Corruption and unknown journal versions fail closed without replacement or truncation.
  This predeployment revision uses format 2 for the separate uncertainty latch; format 1
  is refused rather than silently reinterpreted. No live root journal has been installed.

## Trusted settlement boundary

`release_settled(run_id, fence, observation=...)` is a trusted-controller-only transition
for launched stopping/quarantined work. It requires an `EmptyObservation` from the
root inspector, matching run, UID and boot, and an unchanged current boot. It preserves
the historical launch/uncertainty flags, marks the row stopped with reason
`server_settled`, and never makes the UUID launchable again. Repeating the same fenced
settlement is idempotent. There is no method accepting an agent's `stopped=True`, idle
report or user-cgroup emptiness as proof.

Before calling it, the controller must persist the evidence acknowledgment and check
fresh authoritative state: matching box/journal UUID, counter equal to the fully
acknowledged local ledger, no pending mutation, and this run absent from server-active
runs. Acknowledgment alone is not settlement: overrun/quarantined runs remain active.
It must then obtain `Inspector.stopped` on the exact ledger binding immediately before
release. The journal cannot independently authenticate remote settlement and must not
be exposed as a socket action or treated as a recovery override.

Local intent state is not sufficient for external budget refunds or recovery after
missing journals, snapshot restore or possible rollback. Reconcile authoritative server
state and actual system units/root cgroups before starting more work. A changed boot ID
is evidence of reboot, not a universal restore/rollback detector: memory-inclusive restore
can preserve it, and a disk rollback can erase runs admitted after the snapshot. Startup
must compare the server-side last fence/run state or externally recorded run counter and
fail closed when local state is absent/behind while server runs or root units exist.
Unknown historical
runtime consumes the reservation under the server policy. Do not turn a local
never-claimed cancellation into a refund without the complete trusted evidence path.

The physical slice run limit also needs kernel/manager reconciliation, including unknown
units not represented in this database. This journal enforces the intent-side gate,
not a claim that kernel-side capacity reconciliation is already implemented.

Remaining work includes root system-unit creation and binding, installed-launcher
ownership validation, independent BOOTTIME/UTC guards, cgroup.kill and proof capture,
root socket peer checks and role-scoped server integration, actual daemon-home/worktree
isolation, routing/preemption, operator controls, digest and managed acceptance.

Tests exercise concurrent conflicting grants, single launch claim, stop/claim races,
fenced idempotence, restart/boot/deadline retention, dual-clock expiry, private file
handling, ancestor replacement/misconfiguration, corrupt/future/mode state, claimed
versus never-claimed sweep reasons, durable rejected-operation sweeps, explicit
uncertainty before claim and never-claimed cancellation. They execute no root
code, manager commands, coding-agent sessions or credential setup.
