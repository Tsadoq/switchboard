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
validate its configuration/ancestors and reconcile existing root-slice work before
admission. Tests deliberately use private temporary directories as an ordinary user.
The library does not create a root signer, execute a command or confer permissions.

## Implemented invariants

- Private current-effective-UID directory/file ownership, 0700 parent, 0600 database,
  no database symlink or hardlink alias. Unsafe permissions are refused, not repaired.
- SQLite IMMEDIATE transactions plus a partial unique index admit only one nonterminal
  intent per box, independently of the number of grants the server returns.
- Synchronous EXTRA durability protects the launch claim before external side effects.
  The controller must never issue a create request unless `claim_start()` returned true.
- Same run/fence retries return the original record/deadlines and do not create or claim
  another launch. Different fences, principals, UIDs or boot bindings cannot retarget it.
- A launch claim is persisted once before system-unit creation. Crash or lost manager
  replies after that point retain capacity and cannot be cancelled as never launched.
- Both CLOCK_BOOTTIME and UTC deadlines are checked. Boot change, either expired bound
  or insufficient remaining stop margin quarantines the intent and requests stop.
  None of those conditions proves termination or releases a claimed slot.
- Stop requests and explicit uncertainty retain the gate for claimed work. Repeated stop
  requests do not erase quarantine. All grant/fence dataclass representations hide fences.
- An explicitly cancelled intent that was never launch-claimed can become terminal.
  A concurrent claim/stop transaction cannot mark a claimed launch as never launched.
  The terminal UUID remains recorded and cannot be replayed for another creation.
- Corruption and unknown journal versions fail closed without replacement or truncation.

## Deliberately absent

There is no release/settlement interface for launch-claimed work in this slice. The
future root-owned-cgroup inspector and trusted controller must supply actual binding,
stop and termination evidence before such a transition is added. There is no method
that accepts an agent's `stopped=True`, idle report or user-cgroup emptiness as proof.

Local intent state is not sufficient for external budget refunds or recovery after
missing journals, snapshot restore or possible rollback. Reconcile authoritative server
state and actual system units/root cgroups before starting more work. Unknown historical
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
handling, corrupt/future state and never-claimed cancellation. They execute no root
code, manager commands, coding-agent sessions or credential setup.
