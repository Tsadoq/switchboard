# Night verifier server authority

This implementation is not an enrollment or deployment record. The pilot stays off.
The dedicated endpoint is POST /api/night/verifier. It uses the existing signed
SSH envelope wrapper, timestamp and nonce verification. It never uses a read token
or the generic signed-action dispatcher.

## Role declaration

BOARD_NIGHT_VERIFIERS_FILE names an operator-managed JSON file. Its mapping is from
exact verifier principal to an object containing only box and fingerprint. The
fingerprint is the SHA256 SSH key fingerprint of the separate root-only ed25519 key.
The configured principal must be verifier@BOX. That spelling alone grants nothing:
the principal, configured box, registered signature and actual verifying key
fingerprint must all match. Operational hosts cannot be configured as candidates.

Deploy the role policy before importing its public keys. Never register a root
verifier key as an ordinary coding agent and add its role later. No code here
creates a key, adds a signer or installs a service. Missing/malformed role files
fail startup instead of falling back to agent permissions.

Configured service identities cannot use any generic signed action, issue/redeem
read tokens, or authenticate with bearer tokens issued before their role assignment.
They are excluded from the canonical coding-agent registry while remaining known
to cryptographic signature verification. Unconfigured existing coding-agent
behavior is unchanged.

## Wire actions

The envelope action selects the operation. Every payload has exact keys.

- night.state, target self, payload {}: returns box, journal_id (possibly null),
  counter and active records. Each record includes id, principal, fence, state,
  deadline and the prior boot/invocation/cgroup binding if one was recorded.
- night.bootstrap, target self: journal_id and receipt_id. Creates the server's
  permanent journal identity only if no identity or unfinished runs exist for
  that box. A different existing journal UUID is never silently replaced.
  Replies and retries contain fresh state, not a cached old counter.
- night.grant, target run UUID: fence. Returns exactly principal, box, state,
  remaining_seconds and window_end_utc. Eligibility and remaining time are computed
  from current server control, enrollment, reservation and window state.
- night.binding, target run UUID: fence, journal_id, counter, receipt_id, boot_id,
  invocation_id and cgroup. Counter is the last acknowledged value. This atomically
  checks eligibility, starts the existing reservation, records the unique nonzero
  binding and advances the box counter. Reply: journal_id, counter, state.
- night.evidence: the binding-shaped payload plus outcome (empty or uncertain),
  runtime_seconds (finite nonnegative number or null) and reason. It requires the
  exact previously recorded binding. Reasons are completed, deadline, off,
  preempted, recovery, proof_incomplete and guard_lost. Unknown runtime charges
  the full reservation. Settlement, counter advance and receipt are one transaction.

Every new mutation has a new canonical UUID receipt. Persist the original complete
payload before sending. Retry with identical payload and receipt but a fresh
signature nonce. An accepted mutation returns its original acknowledgment without
reapplying it; a changed payload under that receipt is refused. Bootstrap state
reads are deliberately fresh. Counter/journal mismatches return 409, not a reset.
Role/key failures return 401. No request accepts a command, path selection, UID,
arbitrary charge, enrollment change or stopped boolean.

## Trusted client and physical evidence

NightAuthorityClient signs canonical envelopes with an existing root-only key,
uses the fixed HTTPS board URL, disables environment HTTP configuration and follows
no redirects. Its default signer refuses ordinary-user operation and unsafe key
ownership/permissions. It does not create credentials or negotiate generic tokens.
Tests inject temporary signers and local HTTP transports, never production keys.

The configured Slice= value stays switchboard-night.slice. Systemd interprets
its hyphen hierarchically: the actual cgroup prefix is
/switchboard.slice/switchboard-night.slice. Both ancestors and their cgroup.procs
files must remain root-controlled; treating the slice name as a root-level path
would inspect or kill the wrong location.

A binding report is trusted root-controller input, not proof supplied by a coding
agent. The live implementation must obtain it from Inspector.capture and persist
it before native exec. Empty evidence must come from Inspector.stopped for that
same binding. A socket stopped response, an agent observation file, idle telemetry,
an expired grant or a missing unit alone cannot generate this evidence.

The controller measures the entire grant RPC using CLOCK_BOOTTIME, subtracts it
from the returned remaining time and never renews an existing local deadline.
Startup must reconcile the local journal UUID, acked counter, pending receipts,
server-active runs and actual root slice. A rolled-back/missing journal is not a
fresh machine. Memory-inclusive restore can retain boot_id, so boot change is not
a universal rollback detector. Native execution needs both the server binding ack
and the current guard invocation's arm receipt for the immutable deadlines.

The native launcher, authenticated isolated Happier home, managed installation,
role/key registration, real system-unit evidence and pilot enrollment still need
reviewed integration and acceptance. This API alone does not complete the pilot.
