# Private non-root local API compatibility fixture

This distinct test-only slice follows PR52's toy fixture. It calls the existing production journal_latch.latch and EmissionLock APIs together in fixed exec workers against private fixture data. It changes no shipped source, schema, caller/factory, gate reader, role, service, root release, model/native runner or operational setting.

## Actual versus synthetic facts

Fixture roots are temporary mode0700 directories beneath the actual HOME, avoiding a world-writable /tmp ancestor. LocalJournal constructs a real version 2 journal and provisions synthetic local data before any worker exists. Its constructor/migrations/legacy five-second timeout are outside the exclusion interval. The grant/principal/UID/fence and claim_start bit are synthetic local fixture inputs, not a Board grant, enrolled identity, launched native session or completion witness. Expiry cases deliberately set fixture deadlines to zero while setup is quiescent; the worker still reads the real clock.

Permission/owner/stat/symlink/ancestor checks are NOT mocked. The local production checks allow safe ancestors owned by root or the effective UID, an immediate private parent owned by that UID, and private regular single-link files owned by that UID. The tests run only as non-root and refuse to execute a worker as root. They do not prove immutable privileged deployment, authentication or defense against day agents with root access.

Negative cases change only their own fixture files/directories: unsafe mode bits, hardlinks and symlinks produce the actual production refusal. Unsafe journal paths return NOT_ATTEMPTED/validation_or_path_failure without opening a database; an unsafe lock refuses before entering the mutation interval. No fake metadata or production validation bypass is supplied.

## Production API and descriptor evidence

The worker observes an entry descriptor whitelist of stdio plus one passed result socket, constructs the real EmissionLock, and calls only the bounded existing latch inside held(). SQL tracing/fault injection wraps the actual SQLite connection with a subclass, retaining real SQLite behavior. No LocalJournal constructor, migration or refresh factory runs there. All report sends, acknowledgements, process launches and waits are outside the interval.

Results preserve every production mutation/resource classification and changed flag, with record state only, not a serialized grant/fence. A separate post-exit read reports actual current journal state. UNKNOWN is never rewritten from that read or from process exit. Before/after COMMIT and close faults, combined commit/close faults, update errors, and rollback-plus-close uncertainty exercise the actual latch implementation. Failed first rollback starts no follow-up transaction. Valid and expired stale-fence cases distinguish primary rollback from a fresh committed expiry.

The result reports actual process FD observations after the API returns. A close-before fault can leave real SQLite handles live even after held() has released the order. A paused reporter lets the parent acquire/release the actual production lock while the worker is alive, including with a retained database descriptor. This demonstrates why order release alone is not resource containment. The production coordinator descriptor is also deliberately retained until worker exit. The parent may retain its own coordinator descriptor until the test process exits; no universal all-descriptor-closed claim is made.

The reused OwnedChild fixture associates a pidfd with its unreaped owned child, waits and checks actual exit. Parent probes happen before acknowledging the paused reporter, without a lock held across launch or process wait. Nested coordinator entry and replacement of an already-pinned inode refuse. A new arbitrary-duration holder/enqueue/stop integration is not supplied.

## Distinct result exchange, no authority gate

LOCAL_API_COMPATIBILITY_V1 is a strict capped report schema, not the toy gate's NORMAL certificate. It does not repurpose toy_latched or issue an IDLE/gate-clear transition. Each worker has a new private socketpair, closed-FD exec and a run binding. The parent retains the sender guard, waits for associated worker exit, then requires one nonempty strict record followed by EAGAIN. Empty, extra, oversized, malformed or ancillary records refuse, and delivered descriptors close.

The transport conveys trusted fixture observations, not a public authenticated root protocol. Installed process provenance, hostile same-UID isolation and all-participant descriptor inventories remain unproved. SQLite fault hooks are fixed test-only operation/ordinal/side data; they are not production callbacks or configuration.

## Boundary of the evidence

Success, idempotence, stale-fence/fresh expiry, actual production coordinator release and uncertainty classification are compatibility evidence only. No production worker service, persistent operation gate, external maintenance/restart fence, lifecycle/restore mechanism, shared-ledger order, producer hookup, native stop, manager freshness or physical settlement is implemented. There is no charge refund, slot release, wake acknowledgement or whole-run completion inference. No hard wall-time guarantee or bound on uninterruptible death is claimed.

Owner validation: 38 focused console-entrypoint cases and 1814 full delivered cases pass with isolated HOME and short disk-backed basetemp; the full run has five existing multithread-fork warnings. Ruff/format and mypy173 pass, including both new test/helper files. These are owner results, not independent review or operational certification.

Root smoke remains the authorized ops session's separate work. This fixture neither exercises it nor routes around a session permission restriction. Fresh exact-head independent source review is required. Ticket11 remains owned and disabled.
