# Isolated peer-refusal descriptor evidence

This is a test-only repair for the descriptor-disposal assertion in the peer test suite. Production peer, verifier, worker and caller code is unchanged. It does not authorize a listener, privileged execution, pilot activation or deployment.

## Failure observed

Main CI run43470311062 failed an equality comparison between the pytest worker's complete descriptor sets around a refused frame. The diff contained only descriptors missing from the later set:22 through38 and41. That does not establish a received-descriptor leak. It also cannot establish absence of a leak if an unrelated descriptor closes and its number is reused. The precise cause of those unrelated closures has not been established.

## Replacement evidence

The affected case runs in a dedicated isolated Python process, rather than taking a global snapshot of a long-lived pytest worker with unrelated resources. A separate owned sender sends a real pipe write end through SCM_RIGHTS. Kernel credentials and a pidfd accompany the message. The sender closes its local write end and acknowledges that closure before receipt, then remains alive until the probe finishes.

The production receive function must refuse unexpected ancillary data. Pipe EOF then establishes disposal of the last received pipe writer, not sender exit or whole-run completion. A before/after inventory checks descriptor number plus device, inode and proc target. It excludes the transient proc-directory enumeration descriptor. New or changed descriptor identities fail; unrelated disappearing identities are not mistaken for leaks.

One passing variant deliberately closes two unrelated baseline descriptors before receipt. This reproduces the shrinking-set symptom without a leak. Four negative-control variants deliberately withhold the actual received rights or pidfd from the production cleanup list, with and without unrelated closure. They must fail the proof. The reuse variants additionally require the retained descriptors to occupy baseline numbers, demonstrating why a descriptor-number subset check is insufficient. Intentionally retained descriptors are closed during fixture cleanup, and the owned sender is reaped.

Only the affected assertion is replaced. Other preexisting in-process descriptor-set checks are not rewritten, and this is not a claim that all resource-sensitive tests are now immune to unrelated cleanup.

All process and descriptor operations are ordinary non-root fixture operations. They establish neither authenticated supervisor assertions nor native-run containment, current manager state, installed-writer exclusion or historical stop.
