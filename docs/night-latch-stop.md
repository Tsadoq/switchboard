# Live latch-stop semantics

Source correction for the live latch-stop contract in board thread 31: artifact `89597046-b42f-4f91-a836-806bebc03052`, SHA-256 `76727464da6ec4098c9bca3d57d6a33be444172b19b648cf9277f0eebf49ced4`. Ticket 11 stays with pi@dev; the pilot is off. This change corrects docstrings and adds regression tests. It changes no executable statement, and it is not a stop implementation, a maintenance procedure or a request to run anything.

## What the tool does today

`night.latch.latch_stop` checks its arguments, then reads the configured verifier socket and verifier service from the manager, and only then opens the legacy `LocalJournal`. It requests stop for one named, fenced run, or (`--all-open`) for every run the journal lists as open. It queries no other unit and issues no stop, kill, mask or unmask.

The checks come before the open because constructing the journal can already create, refresh or recover legacy state. That ordering describes current behaviour. It is not permission to open files whose state is in doubt.

## What it does not establish

- **Not all-writer exclusion.** The verifier is not the only journal writer. The guard (`active()` and `request_stop`), the controller's `reserve` and `claim_start`, gate and exec journal construction, root latch paths and the expiry refresh can all write. A stopped-unit observation holds for one instant. It excludes no later restart, raw process, retained handle or root starter.
- **No admission barrier.** The tool does not set up a persistent admission barrier, and it does not keep excluding new starters. The caller must establish no-new-admission and no-new-start ordering over the whole interval. The current unit checks do not provide that.
- **The guard is not a precondition.** Run units are created with `BindsTo` and `After` on the guard. Requiring the guard to be stopped before latching would ask systemd to end runs before their stop request is persisted, which reverses latch-before-kill. In the live protocol, no run may be killed before the required latch transaction succeeds.
- **No certificate.** A successful return, `already` or `no_run` is not proof of a native stop, current global admission state, settlement or universal emptiness.

## Refresh effects inside the call

The journal constructor, `active()` and each `LocalJournal.latch_stop` transaction run the expiry refresh first. Its effects depend on the run:

| Run before the call | Effect of the refresh | What the latch reports |
| --- | --- | --- |
| Never claimed, not uncertain, deadline passed or boot changed | `stopped` with `never_claimed_expired` or `never_claimed_boot_changed`. It leaves the active slot. | Named: `already`. All-open: the run is not listed, and `no_run` may be true. |
| Claimed or uncertain, deadline passed or boot changed | `quarantined` with `deadline_passed` or `boot_changed`. It keeps the slot. | `already`. |
| Unexpired, reserved or starting, not yet stop-requested | No change. | `latched`. The run becomes `stopping` with `stop_requested`, and keeps its slot. |

So the call can release slots, through the refresh, even though the latch update never does.

The latch update itself never marks a run stopped and never releases a claimed run. That run stays `stopping` or `quarantined` until separately authorized evidence and settlement. An uncertain run keeps its slot and its full charge. A latched never-claimed reservation is refused by `claim_start`.

The result fields mean less than they appear to:

- **`already`** means only that this call did not change the latch. It is not a physical-stop certificate or evidence that every writer is absent.
- **`no_run`** describes the legacy journal view only. It does not cover every outstanding native or server obligation, and it can follow a refresh that has just stopped an existing reservation.
- **All-open enumeration** is one transaction, and each per-run latch is a separate transaction. Together they are not one atomic all-open operation.

## Refusals after open

A refusal after the journal opens does not mean the files are unchanged. In particular:

- the reopening constructor may already have committed a refresh;
- on a wrong fence or a missing run, `LocalJournal._transaction` rolls back the refused transaction, then commits a fresh expiry refresh before raising.

## Maintenance is separate

Live latch success cannot be substituted for a maintenance proof. Maintenance needs all of the following:

- a named, trusted no-new-starters and admission provider;
- refusal of every open or uncertain obligation;
- persistent fail-closed restart barriers;
- separately demonstrated absence of the guard, run, gate, exec, verifier, manual, root, retained-handle and any other writers.

Its limits:

- Masks cover managed units only.
- A process scan confirms one instant. It is neither exclusion nor lineage.
- `BindsTo` requests a stop. It does not prove that every owned process or escaped descendant is dead.

`latch.unit_not_stopped` accepts `loaded` and `not-found` and refuses `masked`, and that behaviour is kept. A future proof that a masked unit is stopped needs its own contract and tests. The shared helper must not be loosened silently. Restore and cold owner adoption remain unsupported.

## Legacy limits and UNKNOWN

The legacy CLI and `LocalJournal` transaction paths are not the bounded V4 coordinator writer:

- they do not implement its typed result, cleanup or UNKNOWN discipline;
- this change does not make them bounded, compatible with all-writer emission ordering, or approved for the pilot;
- legacy journal transactions must not be called under a lock and then described as bounded.

Every COMMIT exception remains UNKNOWN, whatever happens afterwards (landing, rollback, a later close, the file bytes, or later observations). These docs authorize no retry, candidate-token or completion probe, reconciliation, cold adoption or compensation. This change does not implement the missing exception classification. Keeping the slot and the full charge, and the physical-stop and settlement gates, are operational obligations. The `Outcome` JSON proves none of them.

## Evidence in this change

- **No executable change.** For `night/latch.py` and `night_state.py`, the production AST is identical to main once module, class and function docstrings are stripped. Nothing else is stripped.
- **`test_night_latch_semantics.py` uses real files.** It runs on real SQLite files in a private directory under `$HOME`, closes every connection it opens, and mocks no ancestry, ownership or permission check. Manager output and cgroup text are fixture models, not real systemd or native acceptance.
- **What the tests cover:**
  - only the verifier units are queried, all before the open, while the guard model is active;
  - no process-control call is made;
  - named claimed and never-claimed latches, including repetition;
  - expiry and boot change of a never-claimed reservation, observed as `already` and `no_run`;
  - quarantine of an expired claimed run;
  - stops committed by the reopening constructor and by the refusal path before a wrong-fence or unknown-run refusal;
  - every pre-open verifier refusal, with the journal directory left uncreated;
  - `loaded` and `not-found` acceptance;
  - the exact `Outcome` keys and exit codes.
- **Existing suites.** The existing latch and journal suites are unchanged and pass.
