# Non-root containment fixture

This is evidence for the bounded candidate in thread31 document disposition1564, not an installed containment mechanism. The only executable additions live under tests. No production caller, journal API, final_snapshot, ledger, role, root release or model/native runner changes.

## What actually runs

A dedicated unprivileged driver process sets Linux PR_SET_CHILD_SUBREAPER on itself, starts a supervisor, and retains cleanup/reaping ownership of orphaned fixture descendants after deliberate supervisor crashes. Neither the pytest process nor any service is made a subreaper. No capability, UID, supplementary group, token, service property, provider configuration or machine-wide setting is changed. Fixtures use only their private temporary files and their own subprocesses.

The supervisor opens a private temporary gate store, publishes PENDING and closes that connection before launching a fixed exec worker. The worker has only stdio and its explicitly passed socket at entry. Its observed /proc/self/fd inventory is in the strict result and checked against that exact whitelist. It forks no descendants and passes no descriptors. The result parser rejects SCM_RIGHTS and closes delivered descriptors; a direct descriptor-marker test observes that closure.

OwnedChild associates a pidfd while the subprocess is still its unreaped owned child. There is one synchronous reaper per owned child, no ignored SIGCHLD or other fixture reaper thread. waitpid status and pidfd readiness supply actual exit observations; EOF never does. Driver cleanup signals only its own remaining children via associated pidfds and reaps them. These are Linux fixture facts, not proof about installed service descendants, old entrypoints or a native night run.

## Deliberately synthetic pieces

The worker mutates a one-row `toy.sqlite3`, NOT the production journal or PR50 latch. It demonstrates process-owned SQLite handles and transaction/cleanup knowledge, not production schema/deadline/expiry compatibility. The fixture lock is a provisioned private flock inode, NOT the production EmissionLock trust-path/coordinator implementation. It does not weaken or monkeypatch production root-ownership checks.

SimulatedLifecycle explicitly labels generation, external maintenance and installed-participant fencing inputs as simulated. The eligible label says `FIXTURE_ELIGIBLE_WITH_SIMULATED_LIFECYCLE`, never operational admission. Known old subprocesses are supplied separately and examined using actual retained lifetime witnesses. A live old supervisor blocks eligibility even when its store contains certified IDLE. The retained-handle test also observes an actual gate-store descriptor in that live process.

Bootstrap evidence has a separate `temporary_fixture_only` simulated authorization label. This is not human authentication or real root provisioning. Restart completion records are trusted fixture-supervisor attestations of prior observations, not persistent pidfds, kernel signatures or proof obtained by looking up an old numeric PID.

## Channel and order

Use a private AF_UNIX SOCK_SEQPACKET socketpair, maximum 4096 payload bytes, capped ancillary buffer, strict JSON object/duplicate-key/field/type/enum/binding validation. Binding includes canonical generation, operation and launch-instance UUIDs. Missing, partial, oversized, truncated, wrong-instance, duplicate and late duplicate results refuse. Unexpected ancillary data refuses, and all delivered descriptors are closed.

The worker checks still-PENDING exact ownership under its flock before mutation. Its result send and any acknowledgement wait are after releasing that lock. Launch and wait are outside the lock. A paused-success test has the parent acquire and release the order while the worker is alive and waiting for its reporting acknowledgement. Late-duplicate exchange uses the same pause, but the extra packet invalidates the exchange.

The supervisor retains its own sender endpoint (`sender_guard`) until exchange validation finishes. This is only a result-channel descriptor, not a database or lock handle. The supervisor does not send result records through that endpoint. After associated worker exit and the audited no-descendant/no-transfer preconditions, there is no remaining result writer. Keeping the endpoint open prevents EOF from masquerading as a zero-length queued record.

The supervisor waits for associated worker exit BEFORE consuming/fully validating the queued exchange. It requires exactly one nonempty strict result followed by nonblocking EAGAIN. Every zero-length receive, whether an empty record or an unexpectedly closed peer, refuses. A second record of any length refuses, so the two-attempt cap cannot skip empty records and hide a later packet. Any unconsumed ancillary rights are released when the rejected exchange sockets close; delivered rights are explicitly closed. Tests observe both delivered-descriptor closure and queued pipe-writer release after an empty-record rejection.

Only complete validation and an observed successful exit can produce a normal certificate. It distinguishes SUCCESS from DOMAIN_REFUSED; UNKNOWN, cleanup faults, invalid/missing packets or unsuccessful exit cannot qualify.

## Gate store and knowledge

The gate has schema version1, one keyed control row and primary-key completion records. Completion bodies are selected only when stored as a BLOB of at most4096 bytes, then strictly decoded. The control reference and complete NORMAL record publish in one transaction, conditionally matching the still-PENDING generation/operation/launch owner. Bootstrap and NORMAL are different record classes; a clear cannot publish a bootstrap record.

Pending set first validates the current certified IDLE. Missing, unsupported, bare, malformed or stale evidence cannot be sanitized by launching a worker. Clearing a stale owner rolls back its inserted completion. Generation mismatch, fault-locked state and current toy observation mismatch refuse startup eligibility. A fresh generation initialization or external fault-recovery mechanism is not implemented.

Gate and worker COMMIT errors remain UNKNOWN even if subsequent rollback/close succeeds. Confirmed commit remains COMMITTED through a close fault. A clear COMMIT-then-error may leave valid durable certified IDLE, which a fresh restart may accept only with the fixture's actual known-old-actor exit observations plus explicitly simulated lifecycle prerequisites. The old supervisor's historical UNKNOWN is not rewritten. A live supervisor with a retained gate-store connection blocks that recovery. Genuine worker faults remain uncertifiable and PENDING; no operator-recovery implementation or blind retry is supplied.

## Coverage and limits

Fault injection is fixed test-only phase/side/crash configuration, not a production callback. Tests compare original knowledge with independent read-only durable observations before/after PENDING and IDLE commits, committed clears followed by close uncertainty, and crashes around body/record publication, launch, worker report, associated exit, complete validation, certificate publication and reply. They cover both normal recovery from certified IDLE after a lost reply and refusal of genuine worker uncertainty. Old process exit never proves historical rollback or native stop.

This tests process crashes and SQLite observations on test storage, not power-loss/fsync hardware behavior, hostile same-UID isolation, immutable root exec/configuration, complete installed/inherited/open/unlinked descriptor inventory, paired restore, all-writer cooperation, manager freshness, enqueue latency, provider completion or native cgroup settlement. There is no hard bound on uninterruptible process death. Test deadlines are orchestration limits; timeout is failure/uncertainty, not a closure witness.

The current console-entrypoint serial targeted run passes100 cases (90 delivered plus10 independent review cases), and the full run passes1786 (1776 delivered plus10 independent), with five existing fork warnings; tracked Ruff/format and mypy171 pass. The independent artifact is untracked and excluded from tracked lint/type claims. Review1574's empty-record reproducer fails3 of10 cases before the fix and all10 pass afterward. Added end-to-end workers send empty records followed by late invalid, duplicate and ancillary-FD packets before actual exit; every such exchange refuses publication.

The initial CI console entrypoint also exposed a collection import failure masked by Python-module invocation. The test uses a sibling import at runtime and the repository namespace only for static typing, without adding a production path.

A fresh exact-head source review is required for this fixture. Even fixture source approval would not authorize a worker/service install, production authority gate, API hookup, root smoke, operational rollout or pilot activation. Ticket11 remains owned and the pilot remains disabled.
