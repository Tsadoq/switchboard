# Unhooked ledger binding publication development

This is private source development under the bounded document dispositions in thread31 posts1744 and1745. Their no-blocking document findings do not approve source, integration, provisioning or rollout. The clarified scope is artifact `d15d3e20-1acd-4154-9513-5076182046bf`, SHA-256 `099a9fdb76bfaef02171418242f3ad4628e9a84a96f18f72bdfc981e5afa0408`. Base is mainab99178, PR60 merged. Ticket11 stays doing, owned by pi@dev; pilot disabled.

## Public types and authority boundary

`publish_binding(path, *, journal_id, claim)` accepts one exact `BindingClaim(run_id, uid, boot_id, invocation_id, cgroup)`. It is an insert-or-match metadata publisher, not an overwrite operation, remote selector or recovery reader. UID is the positive signed-SQLite64 native principal identifier, not the root exec unit's effective UID or proof of a valid Linux account. Invocation is nonzero lowercase32 hex; IDs are canonical lowercase UUIDs. Cgroup must match the existing deterministic unit path exactly, without native inspection.

`StoredBinding(journal_id, claim, observed_server_acked)` is metadata matched in this transaction. The bool `observed_server_acked` is a snapshot of the stored integer0/1 bit, not an authenticated server acknowledgement, native freshness witness or eligibility. It can change after this transaction. No returned field is named `server_acked`, no legacy `RecordedBinding` is returned, and the input has no acknowledgement field. Do not branch on this snapshot as authority for capture, launch, delivery, completion, settlement or refunds.

`BindingWriteResult` keeps mutation, cleanup, changed, stored, refusal and fault independent. Stored metadata is available only on normal insertion COMMITTED or intentional unchanged ROLLED_BACK completion, confirmed closure and no refusal/fault. Every COMMIT exception remains UNKNOWN, even after successful rollback/close or later journal recovery. Failed intentional rollback never becomes successful completion because close subsequently established rollback. Terminal fault precedence is CLOSE_FAILURE over ROLLBACK_FAILURE over the trigger, not chronology.

## Candidate operation

The module independently implements the accepted PR60 file/schema/resource boundary without changing that module or adding a generic transaction wrapper. One fresh existing-file RW connection, timeout0 and three setup statements precede BEGIN IMMEDIATE, V3/delete exact16-object validation, bounded identity validation and one primary-index binding read. Paths, identity and input metadata must originate in trusted local state. File preflight includes path type; it is not claim INVALID_INPUT validation.

Absent inserts five claim fields with literal ack0. Matching claim with existing ack0 or ack1 intentionally rolls back unchanged and preserves the bit. Different valid claim refuses BINDING_CONFLICT. Malformed persisted uid<=0, all-zero invocation or non-canonical cgroup refuses ROW_REFUSED before comparing claim values, even though legacy direct record_binding could persist it. Unexpected INSERT rowcount or post-read state is SNAPSHOT_FAILURE, not late semantic refusal. There is no UPDATE, UPSERT, rebind, unacknowledge, receipt/counter/capture/abort/arm mutation or schema creation/migration/repair.

SIDECAR_PRESENT before SQL is presence only, not hotness, liveness or damage classification. Typed BUSY/LOCKED is BUSY only before successful BEGIN. COMMIT contention stays UNKNOWN. Legacy SQLite openers can recover hot journals as a separate side effect, without a CAS/binding recovery receipt; neither that nor later sidecar absence rewrites the original UNKNOWN. The primitive requires trusted caller-supplied ordering and lifecycle exclusion through confirmed close. Path inode checks do not pin SQLite's actual descriptor or resist hostile same-UID/root actors.

No production caller, existing module, native timer/manager/network path, emission lock or privileged provisioning is changed or invoked. Value equality supplies no epoch/ABA, old-actor non-reuse or continuously current authority. No runtime reconciliation, automatic retry/adoption or settlement is introduced.

## Initial evidence and remaining work

The first32 real private-HOME tests pass with pytest basetemp under world-writable /tmp. Native traces verify insert12, match ack0/ack1 at10, conflict ack0/ack1 at10 and identity refusal9. Legacy direct API insertion demonstrates that uid0/negative, zero invocation and noncanonical cgroup are ROW_REFUSED rather than conflicts. Matching ack1 with before/after intentional rollback failure, compounded with close failures, never returns usable metadata or COMMITs; the existing bit stays1. Real legacy RESERVED/no-journal contention and live journal presence refuse, and a real reader-blocked COMMIT emits13 native statements and remains UNKNOWN after rollback.

Initial owner full validation passes2787 combined tests (five known fork warnings), Ruff/format and strict mypy201. Legacy modules, ledger_arm and every production caller remain byte-unchanged.

This is a development checkpoint, not complete source evidence. Before fresh exact-head source review, complete the input/storage/schema/cap/privacy/inode matrix, all before/after SQL positions and compound faults, post-DML anomalies, actual interpreter exits and old-handle knowledge, and real API branch traces at10/10000/100000 unrelated bindings with indexed SEARCH plans and no changes to unrelated fields/tables. Cap/statement counts do not verify SQLite internal allocation/materialisation, storage latency or wall time. Full/focused/lint/type and pinned export gates need refresh after further changes. No source PR has been opened.
