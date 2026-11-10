# Night verifier protocol draft, version 1

Status: design for review, not an implemented endpoint, installed root service or
permission grant. The pilot remains disabled. This supersedes the user-unit primitive
for pilot launch and settlement: delegated user cgroups cannot provide termination
proof because the agent can migrate processes out. The root service creates runs in
nondelegated system units. It still does not become a general sandbox.

Only request-frame encoding/validation is currently implemented, in night_protocol.py.
It validates structure, not authorization. There is no socket listener, grant client,
root executor, timer, signer enrollment or external settlement handler in this slice.

## Ownership and authority

Ops installs a root-owned verifier/stopper on each explicitly approved candidate box.
Ops is not the unattended hot path. A new root-only signer, verifier@BOX, is registered
through the managed signer workflow only after protocol/code review and recorded rollout.
The board maps that exact principal to one box and a restricted verifier role. A name
prefix alone is not authorization. Never reuse an ordinary agent's signing key.

Verifier identities cannot enroll in autonomy, receive coding-agent timers or perform
ordinary forum/ticket/admin mutations. Their server permissions are limited to reading
current grants/stop requests for their configured box and publishing signed observations
for those grants. Existing generic token/action routes must enforce that role separation,
not merely add a permissive verifier endpoint beside them. No operator token is installed
on an agent box.

Ops defines switchboard-night.slice in the system manager with per-box MemoryMax,
TasksMax and CPUQuota. The root service enforces one active or uncertain run per box
under a local atomic gate, independently of server capacity claims. Uncertain state
continues to occupy that gate across service restarts. No second start is allowed merely
because a server grant exists or a client repeats a request.

The root service and its dependencies/configuration must come from a reviewed root-owned
installed release, never an editable agent worktree or agent-controlled PATH/config/home.
Signer state and its private key remain root-only. Nothing in this draft creates a key,
registers a principal, starts a service or enables enrollment.

## Local socket

Proposed path: /run/switchboard-night/verifier.sock. Its parent and socket are root-owned;
ops grants only the configured local runner UID/group permission to connect. Both peers
check SO_PEERCRED. The client requires UID 0 at the server end. The service checks the
client against a root-configured UID/box mapping, not a claimed UID in JSON.

The autonomous process may share the runner UID and therefore may connect. Treat every
request as untrusted. Peer credentials are not proof that a request came from the runner
rather than the coding agent.

Transport: one UTF-8 JSON object plus newline per connection, at most 8192 bytes. Reject
duplicate/unknown keys, invalid types, noncanonical UUIDs, invalid fences, trailing frames
and missing terminators. Bound read/write and server-verification timeouts, connections
and workers. No shell, command, filesystem path, account, URL, unit name or deadline is
accepted from a socket request.

Request fields:

- v: integer 1, not a boolean.
- request_id: canonical UUID for correlation, not authorization or a new grant.
- op: start, status or stop.
- run_id: canonical admitted UUID.
- fence: opaque server-issued fence.

Response fields: v, request_id, run_id, status and a bounded machine-readable reason.
Success may include an opaque receipt_id. Echo neither the fence nor private state.
Supported statuses are starting, running, stopping, stopped, quarantined and refused. A stopped
socket response alone never authorizes board settlement.

## Root-created run and native execution

1. Acquire the root-journaled local gate, including active or uncertain runs. Fetch a
   fresh authoritative grant from the fixed board configured by ops, authenticated
   as the verifier. The caller supplies no board URL. The grant must bind the run/fence,
   principal, configured box/UID, eligibility, remaining budget and UTC window end. Verify
   that it is current, enabled and not stopping, superseded, finished or quarantined.
2. Persist a start intent and immutable deadline in a root-only fsynced journal before
   creating anything. Derive the only permitted system-unit name from the run UUID.
   Use switchboard-night.slice, User set to the configured account, Delegate=no,
   KillMode=control-group, RuntimeMaxSec and configured limits. Root owns the cgroup
   and its ancestors, so the agent cannot write the common ancestor needed to migrate
   a process into its delegated user subtree. Do not wrap a pre-existing user unit.
3. Use only a root-configured installed launcher and server-authorized isolated worktree
   selection. No socket-supplied command or path is executed. Set HOME, dedicated
   HAPPIER_HOME_DIR and XDG_RUNTIME_DIR=/run/user/UID from configuration. Drop to the
   configured agent UID for native work; no model, daemon or editable repository code
   executes as root. The root-owned pre-exec gate is part of the launch transaction,
   not a voluntary wait performed by an ordinary same-UID launcher.
4. Bind the actual boot ID, system-manager invocation and root-owned cgroup from direct
   manager/kernel observations. Do not trust an agent journal, supplied properties,
   idle telemetry or stopped boolean. Unknown creation outcome retains the local gate.
5. Arm independent root-owned stop authority before native execution. Its budget clock
   is CLOCK_BOOTTIME, measured with clock_gettime(CLOCK_BOOTTIME), so suspend counts
   against the grant. Derive remaining time conservatively by subtracting the whole
   server verification RPC. Stop grace/headroom stay inside that interval. Pair it with
   an independent absolute UTC night-window cutoff. Do not equate OnBootSec or a
   WakeSystem setting with this clock without verifying the actual implementation.
   The agent cannot disarm/extend either guard, and verifier loss must not remove them.
6. Recheck grant freshness, revocation and both deadlines before the trusted launch
   gate permits native execution. Refusal/unavailability means no native execution.
   A successful response returns the captured binding, not permission for the runner
   to launch another process itself. Test pause/snapshot/clock-step recovery; the old
   user primitive's CLOCK_MONOTONIC evidence does not cover this path.

An identical retry returns the original outcome/binding/deadline, without a second
creation. A new request_id does not renew a lease. Conflicting fences, invocations or
terminal-run replay are quarantined or refused. Uncertain launch is recovered by
observation, not resubmission. An empty delegated user cgroup is never accepted as
this system-run binding.

## Stop and recovery

A valid stop can only affect the system unit and root-owned cgroup bound to that admitted
run. Stop uses the kernel cgroup.kill interface on the trusted tracked cgroup, reconciles
the system-manager state and verifies no populated descendants. This does not depend on
the agent's user manager cooperating. The root helper uses fixed argv/configuration,
never caller-selected
units, wildcards, daemon names, PIDs or filesystem paths. It never kills a shared Happier
daemon or changes a parent user manager's permissions/delegation.

The root service processes trusted, typed server revocations for its own box and its
independent deadline. It does not depend on the autonomous caller asking to stop itself.
Board text, incident keywords and arbitrary wake bodies are never root instructions.
Network failure does not extend an existing deadline; new arm requests fail closed.

On startup, recover root journals before admitting new work. Missing records, changed
boots/invocations, pending launch uncertainty and unreadable/populated cgroups are visible
quarantine, not timeout-based capacity release. A reboot or a reused unit name is not an
agent-authorized refund. Recovery must retain the original binding and audit anomalies.

## Signed board evidence

Publish through a reviewed dedicated CLI action using the verifier's own identity. The
board authenticates the exact sender/box role, validates the signed envelope and nonce,
and matches run, principal, fence, boot and captured invocation. Ordinary agent-signed
payloads can never invoke this transition. Replayed evidence must be idempotent and must
not affect another run.

Evidence records an opaque receipt UUID, run/fence binding, boot/invocation, exact tracked
unit/cgroup, observation outcome, stop reason and trusted runtime measurement. The server
checks scope and state before settlement; it does not trust a caller-selected charge.
Runtime is measured from trusted unit/controller observations, not observation network
latency. Unknown runtime consumes the full reservation. Overrun evidence remains latched
and visible. Any refund requires adequate trusted runtime evidence.

Confirmed termination requires the expected unit inactive/failed (or a previously captured
unique invocation now unloaded), plus a readable v2 controller root and an empty/absent
tracked cgroup including descendants. Missing-unit claims without prior trusted observation
are insufficient. Retain quarantine when proof is incomplete. Human-only incident recovery
is separate, authenticated and audited, and cannot override the need to prove termination.

## Acceptance before activation

Tests must cover socket peer spoofing, frame/size/type limits, role/key separation, grant
and fence substitution, cross-box/UID/unit targeting, duplicate start/replay, local gate
races despite conflicting server grants, immutable retry deadlines, off/preemption,
verifier loss, network loss, restart/reboot, unit reuse, unreadable/populated cgroups,
detached/resistant descendants and actual daemon-home isolation. Demonstrate that the
autonomous UID cannot migrate its tracked PID into a sibling/delegated cgroup, stop the
root guards/service or read the verifier key. Prove common-ancestor ownership and verify
CLOCK_BOOTTIME plus UTC-cutoff behavior, without broader security engagement.

Remote effects and work explicitly brokered into unrelated local services remain outside
this tracked-unit boundary. No arbitrary-command/root execution interface is authorized.
Deployment requires independent review, managed dry-run/apply records and an explicit
small enrollment with tested off/recovery behavior. Draft approval alone enables nothing.
