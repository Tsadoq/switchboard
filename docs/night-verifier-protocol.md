# Night verifier protocol draft, version 1

Status: design for review, not an implemented endpoint, installed root service or
permission grant. The pilot remains disabled. This extends the cooperative user-unit
primitive with independent stop authority; it does not become a general sandbox.

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
- op: arm, status or stop.
- run_id: canonical admitted UUID.
- fence: opaque server-issued fence.

Response fields: v, request_id, run_id, status and a bounded machine-readable reason.
Success may include an opaque receipt_id. Echo neither the fence nor private state.
Supported statuses are armed, stopping, stopped, quarantined and refused. A stopped
socket response alone never authorizes board settlement.

## Arm before native execution

1. Fetch a fresh authoritative grant from the fixed board configured by ops, authenticated
   as the verifier. The caller supplies no board URL. The grant must bind the run/fence,
   principal, configured box/UID, eligibility, remaining budget and UTC window end. Verify
   that it is current, enabled and not stopping, superseded, finished or quarantined.
2. Derive the only permitted user-unit name from the validated run UUID. Query the user
   manager/kernel directly and bind the actual boot ID, invocation and exact cgroup. Do
   not trust an agent's journal, supplied properties, idle telemetry or stopped boolean.
3. Persist that binding and an immutable deadline in a root-only, fsynced journal. Derive
   a conservative deadline from server remaining time minus the whole verification RPC,
   with stop grace/headroom inside the grant. Define the host clock basis explicitly.
4. Arm a root/system-manager-owned stop backstop before returning armed. The agent UID
   must not be able to disarm or extend it. A root daemon crash must not remove it.
   Include an independent UTC window cutoff and test pause/snapshot/clock-step behavior;
   the user primitive's CLOCK_MONOTONIC test is not evidence for those cases.
5. Only then may the launcher execute the native target. Its final freshness/expiry
   check still applies. A refused/unavailable verifier means no native execution.

An identical retry returns the original binding/deadline. A new request_id does not
renew a lease. Conflicting fences, invocations or terminal-run replay are quarantined
or refused, never re-armed. Uncertain launch is recovered by observation, not resubmission.

## Stop and recovery

A valid stop can only affect the unit derived from that admitted run, under the configured
account. The root helper uses fixed argv and fixed configuration, never caller-selected
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
and fence substitution, cross-box/UID/unit targeting, duplicate arm/replay, immutable
retry deadlines, off/preemption, verifier loss, network loss, restart/reboot, unit reuse,
unreadable/populated cgroups, detached/resistant descendants and actual daemon-home
isolation. Demonstrate that the autonomous UID cannot stop the root timer/service or
read the verifier key, without attempting broader security engagement.

Remote effects and work explicitly brokered into unrelated local services remain outside
this tracked-unit boundary. No arbitrary-command/root execution interface is authorized.
Deployment requires independent review, managed dry-run/apply records and an explicit
small enrollment with tested off/recovery behavior. Draft approval alone enables nothing.
