# Unhooked journal start claim

Source candidate for the pinned journal start-claim contract in board thread 31:
- artifact `35e5a29d-7466-4c87-b423-5fe5370fd298`, SHA-256 `a19a97b93fa90d07ffad1d68fc61181d49986498a65b7a99e75bdd9eab739f8c`;
- it overrides design draft `1929da72-4c29-4f99-997f-7942496e900a`.

Ticket 11 stays with pi@dev; the pilot is off. This module has no caller.

## What it is

`night/journal_start.py` offers one writer:

- `claim_start(journal_path, *, expected, principals, now, grace)` inserts one new LocalJournal version 2 row for `expected`. The row is `starting` with `launch_claimed=1`, and it is written in one `BEGIN IMMEDIATE` transaction before any native allocation request.
- It returns a `StartClaimResult` with `mutation`, `cleanup`, `refusal` and `fault`. `usable` is a property, true only for COMMITTED, CLOSED, and no refusal or fault.

The result carries no Grant, control, permit or settlement field.

The committed row records a conservative obligation. It is not a native allocation witness and not permission to launch.

## Inputs

Every field is checked before any file or SQL access:
- **`expected`** must be exactly a `Grant`; a `Record` is refused.
- **Run and boot ids** are canonical lowercase UUIDs.
- **The fence** is exactly 43 characters of the fence alphabet. This deliberately narrows the protocol and legacy grammar (32 to 128); existing rows are still read with the legacy grammar.
- **The principal** uses the existing grammar, capped at 128 bytes (the `capture_queue` cap).
- **The uid** is an exact int from 1 to 2^63-1.
- **Deadlines** are exact positive finite floats.
- **`now`** must be exactly a `ClockReading` with nonnegative finite floats.
- **`grace`** is an exact positive finite float. The configured 5.0 is valid.
- **`principals`** must be an exact `dict` of 1 to 64 entries, each an exact ASCII str principal mapped to an exact positive int uid. It is copied into an owned map with at most 65 iterations, and a size change is refused.

Seven grant fields, three clock fields, `grace` and the map are snapshotted before the open. Changing the caller's objects later has no effect.

Semantic refusals come only after all structural checks, in this order:
1. **PRINCIPAL_BINDING:** the selected binding is missing or has a different uid.
2. **BOOT_MISMATCH.**
3. **GRANT_WINDOW_REFUSED:** with `rb` and `ru` the remaining boot-time and UTC intervals, it refuses unless `max(rb, ru) <= 1200` and `min(rb, ru) - grace > 6`. Equality refuses.

The window check is stronger than legacy `reserve`. It keeps both the stop grace and the minimum work interval.

The map is trusted configuration, not authority. Principals may share a uid.

## Files and SQL

The journal must already exist:
- owned by the coordinator's euid;
- mode exactly 0600, one link, private ancestry;
- a SQLite header and no `-journal`, `-wal` or `-shm` sidecar. A sidecar means refusal by presence only: no diagnosis and no clearing.

The header is read and its descriptor closed before SQLite connects with `mode=rw`, `timeout=0` and `isolation_level=None`. With that setting the driver issues no implicit BEGIN or COMMIT. The only transaction boundaries are the explicit `BEGIN IMMEDIATE`, `COMMIT` and at most one `ROLLBACK` in the schedule below, and the PRAGMAs before BEGIN run outside any transaction. After the connect, only `lstat` identity and the mode are rechecked.

The schedule is 13 statements:
1. `PRAGMA busy_timeout=0`, `synchronous=EXTRA`, `trusted_schema=OFF`, `BEGIN IMMEDIATE`;
2. read `PRAGMA journal_mode` (never set), read `PRAGMA user_version`, read the exact three-object catalog;
3. the named run by its primary key, then the active slot by `one_active_run` (LIMIT 2 each);
4. one INSERT with explicit state, flags, reason and slot;
5. the full named row read back (all 13 fields), then the active slot (exactly this run);
6. COMMIT.

The catalog, run and active queries are the `queue_association` constants, pinned by identity tests.

The cap is 14 statements: one ROLLBACK is allowed after a COMMIT exception. Every refusal or fault stops early. There is no DDL, retry, sleep, history scan or expiry refresh, and nothing is observed after COMMIT.

Any existing row for the run refuses RUN_ALREADY_RECORDED, even a stopped or exact one. Any active row refuses ACTIVE_PRESENT, even an expired or quarantined one. A malformed row refuses ROW_REFUSED. A bad rowcount or readback after the INSERT is SNAPSHOT_FAILURE, never a refusal.

Typed BUSY or LOCKED (an exact nonnegative int code masked to 5 or 6) is a refusal only before any successful BEGIN. That excludes a BEGIN that executed and then raised. Every later SQLite error is SQLITE_FAILURE.

## Outcomes

| Boundary | mutation | cleanup | refusal or fault |
| --- | --- | --- | --- |
| No DML attempted | NOT_ATTEMPTED | CLOSED | the refusal or fault |
| No DML attempted, then ROLLBACK or close raises | NOT_ATTEMPTED | UNCERTAIN | any refusal kept, plus ROLLBACK_FAILURE or CLOSE_FAILURE |
| DML attempted, failure before COMMIT, ROLLBACK acknowledged | ROLLED_BACK | CLOSED, or UNCERTAIN with CLOSE_FAILURE | SQLITE_FAILURE or SNAPSHOT_FAILURE |
| DML attempted, ROLLBACK raises | UNKNOWN | UNCERTAIN | ROLLBACK_FAILURE or CLOSE_FAILURE |
| COMMIT raises | UNKNOWN | from the one ROLLBACK and the close | SQLITE_FAILURE or a higher cleanup fault |
| COMMIT returns, close raises | COMMITTED | UNCERTAIN | CLOSE_FAILURE, not usable |
| COMMIT returns, close returns | COMMITTED | CLOSED | none; usable |

How the outcome is decided:
- **Precedence** is CLOSE_FAILURE over ROLLBACK_FAILURE over the trigger.
- **Bookkeeping comes first.** The DML and COMMIT attempts are recorded before each call. A later close never rewrites UNKNOWN.
- **The constructor accepts exactly these rows.**
- **Interruptions.** A KeyboardInterrupt or other uncaught exception still runs the bounded rollback and close, then propagates; it never becomes a result.

## What it relies on and does not prove

**Exclusion.** The caller is one continuous root-euid owner that excludes every journal and ledger reader and writer, and maintenance, through all-writer ordering. No same-process handle to the pair may be open. A separate reader holding SHARED makes the COMMIT fail, so the result is UNKNOWN; this is tested with a real second process.

**No release (C4).** The claim refreshes nothing and releases nothing. One committed claim holds the slot until a separately authorized, bounded release writer exists. So this is a single-use foundation, not a scheduler, and no legacy constructor may serve as the release.

**Lifecycle (C3).** The caller keeps an explicit native-request phase. A `starting` row with an inactive unit means neither "exited" nor "never requested". No legacy finished-inference, sweep or settlement path may consume the row. A repeated protocol request is answered only from the live owner's retained memory. This API never succeeds twice, and a disk read never reconstructs a lost or UNKNOWN lifecycle. COMMITTED with cleanup UNCERTAIN means the obligation is recorded but the caller must not request native work.

**Not supplied here:**
- fresh grant, native and server admission (P2);
- all-reader and all-writer cut-over (P7);
- a trusted initializer and lineage;
- a native request witness;
- bounded stop, quarantine, settlement and release writers;
- whole-run physical-stop acceptance.

## Evidence in this change

- **`test_night_journal_start.py`** runs on real SQLite files in a private directory under `$HOME`, with no ancestry or ownership mocks. It covers:
  - the success row, refusing a repeat, and active, expired and terminal rows;
  - legacy 64-character fences and malformed stored rows;
  - every input field, map bounds and caller mutation during the open;
  - both timing edges, including grace 5.0, equality and 1200;
  - the refusal order before any file access;
  - sidecars, modes, missing files, symlinks, hard links, unnormalized aliases, a non-SQLite header and a swap before connect;
  - schema drift;
  - a real separate-process EXCLUSIVE holder (BUSY) and SHARED holder through COMMIT (UNKNOWN);
  - all 576 constructor combinations against the contract table.
- **`test_night_journal_start_evidence.py`** traces the attempted and native statements. It covers:
  - the 13-statement schedule;
  - faults before and after every statement, including a BEGIN and a COMMIT that land;
  - typed primary, extended and invalid BUSY codes;
  - every ROLLBACK and close compound;
  - each postwrite column changed in value and in type, and the rowcount, cardinality and active mismatches;
  - prewrite row and schema refusals;
  - interrupts;
  - path-scoped descriptor checks and SQLite connection closure, with an unrelated-close regression and true-leak and unclosed-connection controls;
  - 10, 10,000 and 100,000 stopped-history rows, with and without an active row. Those show unchanged SEARCH plans on `sqlite_autoindex_runs_1` and `one_active_run`, and identical VM step counts.

These are fixture tests. They do not certify SQLite's allocation, latency, every corrupt state, owner or uid enforcement under root, or anything native or operational.
