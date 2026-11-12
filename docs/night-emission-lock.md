# Isolated emission-lock primitive

This is an unhooked primitive. It does not change journal or ledger transactions, emit context, add a listener, or authorize any operational action. Those integrations require separate review.

`agent_board.night.emission_lock.EmissionLock` opens an already-provisioned private regular file. It never creates or replaces the file. The caller supplies a trusted absolute configuration path, not a request field. Existing night-state ancestor and effective-owner permission checks apply; production participants must run as root, while unprivileged tests use their own private files. The file must have one link, no group/other permissions and no symlinked ancestors.

```python
lock = EmissionLock(trusted_path)
with lock.held():
    perform_local_nonblocking_work()
```

## Entry and ownership

All objects naming the same device/inode use one process-local coordinator, retained close-on-exec descriptor and thread mutex. Paths and retained descriptors are checked against the pinned identity on entry and normal exit. Missing, unsafe, unlinked or replaced files refuse; runtime never repairs them.

Entry uses nonblocking registry/thread acquisition and `flock(LOCK_EX | LOCK_NB)`. Contention raises `EmissionLockBusy`. Same-thread nesting raises `EmissionLockError("nested_lock")`, including through a different object for the same inode. No acquisition retries or sleeps are hidden in the primitive. A body exception releases ownership.

The coordinator retains its descriptor for the process lifetime. The trusted runtime must use a bounded configured lock namespace, not construct locks from arbitrary incoming names. The primitive deliberately does not expose an object close operation that could invalidate another object's shared coordinator.

## Fork, death and exec

Fork preparation serializes coordinator creation so every inherited owned descriptor is tracked. This fork preparation may wait for registry work; it is not a bounded-time fork API. The parent resumes with its registry intact. The child closes inherited descriptors without `LOCK_UN`, clears ownership and rebuilds local synchronization. Unlocking the shared open-file description in the child would incorrectly unlock the parent.

An inherited context's cleanup does not unlock or release the parent's ownership. A child must acquire a fresh context before protected work. Callers must not continue a parent's protected business operation across fork. Future final emission sections must contain no fork/exec or manager subprocess.

Process death releases the kernel lock. Exec closes the retained descriptor. The isolated tests exercise both, as well as child acquisition while the parent holds the lock and explicit cleanup of an inherited context.

## Limits and later gates

Nonblocking entry is not a hard wall-clock duration guarantee: metadata/filesystem operations and scheduling can stall. Trusted-parent ownership is essential; inode comparisons do not defend against a malicious host root replacing paths at arbitrary times.

This module does not establish cross-database consistency, freshness of manager observations, stop completion, whole-run completion, wake handling or a maximum stop latency. No existing transaction factory imports it. Before integration, independently audit all direct connection/commit paths, define bounded workload and failure behavior, and test mixed journal/ledger invalidations against the same provisioned inode. Preserve the offline latch's positive verifier-death requirement.

The pilot remains disabled until its independent operational and native acceptance gates pass.
