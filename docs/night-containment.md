# Disabled local night containment primitive

`agent_board.runner.night_containment` adds a local unit plan, startup guard and
termination-observation predicate. Nothing imports it into ordinary runner dispatch.
There are no new runner configuration keys, server routes, enrollments, session launches
or authorization grants. The pilot remains disabled and ticket 11 is not complete.

## Local mechanism

A trusted caller constructs a `RunPlan` from a current server grant. `seconds` must be
server-computed remaining time, capped at 1200 and the night-window end, not an agent
request. `request_elapsed` accounts conservatively for the whole grant RPC. The plan
converts the remaining interval to a host-monotonic deadline and binds it to the boot
ID, principal, opaque fence and a canonical unique run UUID.

The plan refuses overlapping ordinary/dedicated homes or working directories, including
resolved aliases. This is not itself proof that a directory is a separate authorized
Git worktree. Repository verification and worktree creation remain integration work.
The command is trusted launcher input, never an agent-supplied shell command or payload.

`RunPlan.save()` uses an exclusive, private, fsynced file and directory sync. A trusted
launcher must save before submitting `unit_argv()` and must never replay submission from
an existing journal after an uncertain result. Recover by observation instead. The
worker also writes its observation exclusively before executing the target, preventing
a second execution under the same journal even if someone retries the unit command.
Partial or corrupt records are uncertainty, not permission to create a replacement.

The main transient user service has a unique `board-autonomy-UUID.service` name, a
fence-hash marker, explicit app.slice membership, control-group killing, SIGKILL after
five seconds, no restart, no new privileges, 600 MiB memory limit, 128 tasks and one CPU.
These are initial conservative limits, not a substitute for reviewed per-host policy.

Before executing the target, the worker checks its actual UID, boot ID, systemd
invocation and kernel cgroup. It checks the immutable monotonic deadline, arms a separate
absolute monotonic stop timer and checks the deadline again. Expired queued activation
never executes the target. The dedicated home is provided through HAPPIER_HOME_DIR.

The five-second stop grace and one-second scheduling headroom are **inside** the grant.
Stop begins at deadline minus six seconds; the SIGKILL backstop is configured to finish
one second before the deadline. Grants too short for this are refused. A systemd timer
is not a real-time scheduling guarantee: observed overruns still require the server's
latched incident quarantine, not a claim that timing failures are impossible.

RuntimeMaxSec is also set at service creation as a relative backstop. Live testing found
that this host rejects changing RuntimeMaxUSec on an existing unit. The absolute
OnBootSec timer is therefore armed before target exec; late unit activation cannot
extend the grant through a fresh relative RuntimeMaxSec. Stop timers run a fixed
systemctl command against that run's unique unit, never a shared daemon. Timer/helper
names are derived from the run UUID and must be cleaned up during trusted recovery.

## Proof and trust limits

`verified_empty()` is an internal predicate over **trusted** manager observations and
kernel cgroup.events. It requires the expected boot, unit identity, prior nonzero
invocation, fence marker for loaded units, inactive/failed state, the exact tracked
cgroup (or its absence), a readable v2 controller root and no populated descendants.
An unloaded unit can count only after the verifier previously observed that unique
invocation and enforces the single-submission journal. Unit absence alone, agent idle
telemetry, an unobserved launch, missing controller root and an unreadable/occupied
cgroup are not proof. A different boot or invocation is not an automatic release.

The worker's local observation file is a recovery hint, **not an authenticated external
attestation**. The board must not accept ordinary agent-signed files/booleans as trusted
settlement evidence. A separate privileged/read-only verifier authority and its
run/fence-bound protocol are still required before exposing settlement or enabling.

This contains ordinary inherited children, including setsid/nohup/SIGTERM-resistant
processes. It is not a general security sandbox: explicitly brokered sibling user
units, remote jobs, pushes and API calls are outside that inherited cgroup. No new
access permissions or claim of containment for those effects is made.

## Evidence and remaining work

Unit tests use blocked native process entry points and simulated manager/kernel facts.
They do not start a daemon, contact an account or alter the live runner configuration.

On a development host with a user manager and delegated v2 controllers, run:

```
uv run python scripts/test-night-containment.py
```

This creates temporary UUID-named user units containing only Python surrogate processes,
not coding agents or Happier sessions. It exercises normal and delayed activation,
setsid/SIGHUP/SIGTERM-resistant descendants, the budgeted kill deadline, active/empty
proof rejection/acceptance, preservation of an unrelated sentinel and expired startup.
It cleans up its exact unit/timer names and writes local JSON evidence. It requires no
new credentials and does not enroll or enable a pilot.

Still required: trusted launch/stop/recovery orchestration and authority, manager/runner
loss cases, actual dedicated/ordinary Happier-home interference tests with approved
device setup, separate session/worktree records, source-split held leasing, preemption,
repository policy, morning drain/digest, authenticated controls and managed deployment.
Do not represent surrogate unit evidence as an end-to-end autonomous pilot.
