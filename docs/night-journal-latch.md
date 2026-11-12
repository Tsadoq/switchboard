# Unhooked journal latch source proposal

Status: isolated source proposal implementing the bounded contract reviewed in thread31 post1541. No existing factory or runtime caller uses this API. It does not establish an ordering interval or an operational containment policy.

## Preconditions and exclusions

The trusted caller must already exclude cooperating writers and all database/sidecar/inode replacement or maintenance from connection establishment through confirmed close, including the rollback gap between transactions. Current legacy participants do not honor this order, so production does not yet satisfy it. File ownership checks and SQLite transactions do not prove that an independent actor cannot rename a database.

The path, run ID and fence come from trusted root state. This API never acquires the emission primitive, creates a database, calls a legacy store, migrates, enumerates history, contacts a manager/network service, enqueues a frame or claims physical stop/completion. Tests invoke it only against private temporary fixture databases without root/native execution.

## API and knowledge model

`journal_latch.latch(path, run_id=..., fence=...)` returns `LatchResult`.

Primary and follow-up expiry outcomes are independent: NOT_ATTEMPTED, COMMITTED, ROLLED_BACK or UNKNOWN. NOT_ATTEMPTED means the data body was not entered; a connection/transaction might still have required cleanup. Cleanup separately reports CLOSED or CLEANUP_UNCERTAIN.

A normally returning COMMIT establishes COMMITTED even if later close raises. Any COMMIT exception leaves UNKNOWN, including errors injected after underlying commit. Subsequent rollback/close or in_transaction false cannot turn that ambiguity into unchanged state. Reported changed fields are None for UNKNOWN, False for confirmed rollback/not-attempted and the actual attempted effect only after confirmed commit. `latch_changed` describes the latch itself; `primary_changed` also includes any expiry update in that primary transaction.

A committed mutation with cleanup uncertainty retains its committed/changed facts but returns no authority-bearing Record. Only primary COMMITTED, cleanup CLOSED, no fault and no domain refusal returns the selected Record. This Record is a local latch result, not a kernel witness or settlement authority. Even an unchanged stopped record is not physical-stop proof.

`domain=stale_fence` is diagnostic unless all relevant outcomes and fault status establish the refreshed refusal. It does not by itself assert that expiry committed. Only syntactically valid missing targets and well-formed wrong fences enter this path. Invalid selectors, unsafe paths, schema/mode/storage/clock faults and SQLite errors do not trigger a second expiry pass.

After confirmed first rollback, the follow-up starts a new transaction, validates mode/version/schema again, and calls the fixed trusted clock helper afresh before its indexed expiry lookup. Failed first rollback never starts that transaction. The deliberate narrowing of legacy catch-all StateError refresh semantics is isolated here; compatibility review is required before replacing any caller.

## SQL and data assumptions

- Existing-only `mode=rw`, timeout0, busy_timeout0, synchronous EXTRA and trusted_schema OFF.
- DELETE mode and journal version2; no journal-mode setter or automatic migration.
- Exact normalized table/primary-index/partial-index schema, checked inside each begun transaction using capped metadata.
- Active query forces one_active_run with slot1 and state not stopped, LIMIT2. Target and post-update queries use the run_id primary key with LIMIT2. Updates target the selected primary key.
- Byte-capped SQL BLOB projections before decoding: run/boot UUID36, fence256, principal255, state16, reason256. Numeric storage and integer boolean/slot representations are checked before creating a Record.
- Fixed ClockReading validation; quarantined rows are not refreshed again. Expired/boot-changed never-claimed nonuncertain reservations stop, while claimed/uncertain runs quarantine, preserving legacy reason selection.
- No automatic retry of BEGIN, COMMIT, mutation, rollback or close. A failed rollback is not repeated. A confirmed close can establish rollback of a never-committed open transaction but never retroactively authorize a skipped follow-up.

Proposed explicit-statement ceilings remain13 normal,20 eligible domain-refusal and21 with cleanup. The preamble is3 statements; each transaction adds BEGIN,3 validation statements, bounded data queries/updates and COMMIT. The second pass includes its own validation. Close and any implicit rollback are separate resource operations. Statement ceilings and indexed cardinality do not bound disk I/O, B-tree height, clock-file reads, scheduling, fsync or stop latency.

## Delivered evidence

121 targeted tests pass. Coverage includes:

- primary commit/idempotence and actual fresh second expiry for missing/wrong-fence selectors;
-80 combinations of five legacy states, claim/uncertainty flags and nonexpiry/boot/BOOTTIME/UTC conditions, comparing durable rows with the existing expiry helper;
- before/after underlying primary/follow-up COMMIT, first/cleanup ROLLBACK and CLOSE faults, keeping observed data separate from reported knowledge;
- second BEGIN/validation/update failures, schema change during the rollback gap, invalid second clock and corrupt historical target refusal;
- validation failure after BEGIN and before/after-underlying-BEGIN cleanup without entering the data body;
- external RESERVED/EXCLUSIVE writers and a DELETE reader that allows UPDATE but blocks COMMIT;
- exact SEARCH plans using one_active_run and sqlite_autoindex_runs_1 with10,10000 and100000 historical rows;
- missing/unsafe files, invalid selectors and no creation or unrelated authority changes.

Saved representative traces measure12 statements for normal latch,17 for a later fresh expiry refusal,18 when both expiry passes attempt updates,12 for an ambiguous post-commit fault,19 for follow-up commit plus cleanup-rollback faults, and12 for committed-plus-uncertain-close. These observed branches are below the proposed ceilings, not a claim that every theoretical maximum is reachable.

Initial full delivered rerun passed1675 tests, but CI independently reproduced the earlier existing stub-test /proc/stat ProcessLookupError race. The added test-only cleanup repair treats ESRCH like ENOENT in that probe, with deterministic coverage for both and for PermissionError still propagating. It changes neither production acceptance_stub behavior nor the latch API, and provides no operational stop evidence. The full delivered suite after this repair passes1678 tests with five existing multithread-fork deprecation warnings. Targeted121 latch cases, Ruff/format and mypy169 pass. These are owner-delivered results, not independent certification.

## Remaining gates

Cleanup/commit uncertainty still needs a reviewed process containment/reconciliation policy before production. Startup maintenance/restart exclusion, installed/open-descriptor inventory, journal+ledger/sidecar backup reconciliation, release-wide mutation/file audit and migration fencing remain unresolved integration work. Ledger writers, other journal mutations and legacy read-refresh paths are not covered by this new API. Manager freshness, enqueue ordering and stop-failure/latency policy are separate gates.

Next: request a new explicit exact-head independent source review with traces/query plans/history-scale/fault evidence, fix findings and rerun normal gates. No future reviewer acceptance, factory hookup, service/property change, credential, root run or activation is inferred. Ticket11 remains owned by pi and the pilot remains disabled.
