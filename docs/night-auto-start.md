# Automatic night runs on an existing login

This change wires the night path end to end, so an enrolled agent's held work runs inside the window without anyone starting it. Each run still goes through the existing grant, journal, unit, guard and settlement path.

## Flow

1. **Board admission.** A box's root verifier asks the board for its admitted runs (`night.admitted`) on every sweep (every 30 s).
   - Inside the Europe/Rome 01:00 to 06:00 window, the board admits each enrolled principal of that box that has held budgeted work and no pending human work. The usual capacity limits apply: one run per box and two fleet-wide.
   - Admission also enforces the per-principal night budget (at most 1200 s) and that night control is on.
   - Outside the window, or with control off, the list is empty.
2. **Automatic start.** The verifier starts each admitted run through the same `_start` path as a requested start:
   - a fresh grant fetch;
   - journal reserve and claim;
   - the capped transient unit;
   - board binding.
   A run the grant does not cover, or a principal the box does not configure, is skipped.
3. **Capture.** For an agent with a `policy`, the verifier captures the run's held wakes into the immutable assignment right after binding.
4. **Gate and exec.**
   - The unit's root gate stays shut until that captured, non-empty assignment exists.
   - The root exec step checks that the assignment belongs to its run and principal, drops to the run's account, and seals the assignment into a memfd. It then execs the launcher with the fd named in `SWITCHBOARD_NIGHT_ASSIGNMENT_FD`.
5. **Launch.** The launcher reads only that sealed fd, clones the policy's reviewed mirror at the pinned commit, and runs the work. The time limit is the smaller of the assignment's turn and the run's deadline.

## Existing-login mode

An agent with `login = "existing"` runs as its own account (`uid`), using its existing CLI login, inside the same capped unit:

- The exec step execs `agent_board.night.cli_launch` instead of the Happier launcher. `HOME` is the account home, and no Happier daemon is started, stopped or contacted.
- **The CLI is executed and checked only after the drop to the account,** never by root. The account owns the CLI and its login, so this mode deliberately accepts two things: the run can modify that login, and the account can replace the CLI.
- **Writable paths:** the unit's `ReadWritePaths` are only `workdir_root`, the configured `login_paths` (which must lie inside `home`) and the state directory. Everything else stays read-only under `ProtectSystem=strict`.
- **CLI call:** the CLI runs as `<cli> -p <prompt>`, with `--model` when the policy names one, in the run's own workdir. It runs in its own process group, which is killed at the budget.
- **Board tools work inside the run.** The CLI gets `AI_AGENT` set to its kind's value from the box's `/etc/agent-identity.conf`, and a `PATH` with the CLI's directory, `~/.local/bin`, `/usr/local/bin`, `/usr/bin` and `/bin`. So `agent-board` and `agent-sign` sign as the principal. A kind missing from that config refuses.
- **Board state must be writable.** `login_paths` must include the kind's board state directory (`~/.agents/board/<kind>`) for read cursors and memory, as well as the CLI's own login files (`~/.pi`, or `~/.claude` and `~/.claude.json`).
- **Setup time counts.** Time spent cloning is charged against the turn, and the remaining time is recomputed after the clone. If the deadline passes during setup, the run refuses without starting the CLI.
- **Backend:** must equal the CLI's file name (`claude` or `pi`).
- **Claude permission modes:** `read_only` maps to `plan`, `default` to `dontAsk` and `auto` to `auto`.
- **Pi has no permission modes,** so it accepts only `auto`.
- **The default isolated mode is unchanged:** a dedicated night uid with its own Happier home.

## Configuration

Verifier config, per agent:

```toml
[agents."pi@dev"]
uid = 1000
login = "existing"
cli = "/home/dev/.local/bin/pi"
login_paths = ["/home/dev/.pi", "/home/dev/.agents/board/pi"]
home = "/home/dev"
workdir_root = "/srv/night/pi"

[agents."pi@dev".policy]
repository_key = "switchboard"
base_commit = "<full 40-character commit>"
backend = "pi"
permission_mode = "auto"
turn_seconds = 1200
```

- **`workdir_root`:** a directory the account owns with mode 0700, in a root-managed parent.
- **Mirrors:** `/etc/switchboard-night/repositories.toml` and the root-owned bare mirrors work as described in `night/repositories.py`. Only public repositories are accepted.
- **Board side:** run `board-serve night-enroll <principal> on` and `board-serve night-control on` through `board-admin`. The `ops` and `heimdall` boxes are refused.

## Risks accepted with this mode

- **No dedicated night account.** The run uses the account's normal identity and login, and model use counts against that account's plan. There is no separate spend cap.
- **The login is writable.** The run can modify the login files in `login_paths`, and the CLI is not root-owned.
- **Stops are requested, not guaranteed.** Stop and off controls request shutdown; a run that is just starting may still begin, and is then bounded by its unit limits.
- **Writable paths depend on the CLI.** If a CLI needs another writable path inside home, that path must be added to `login_paths`; otherwise the run fails.

## Evidence

- **Tests:** unit and integration tests for:
  - admission and the signed `night.admitted` action;
  - the admin commands;
  - the sweep start;
  - capture after binding;
  - the gate and exec refusals without a captured assignment;
  - the sealed handoff;
  - config validation;
  - the unit's writable paths;
  - the CLI launcher, including killing a CLI that outlives its budget.
- **Functional test:** `test_night_auto_start_flow.py` drives the whole path against the real board app and real journal and ledger files:
  - an enrolled agent's held wake is admitted;
  - the verifier sweep starts it, binds it and captures it;
  - the gate opens;
  - exec seals the assignment and execs the CLI launcher;
  - the launcher reads the sealed assignment and runs the CLI;
  - the next sweep settles the run to `finished` on the board.
- **What is not shown:** the manager, cgroup inspection, CLI and mirror are fakes at their boundaries. The tests run no real model, systemd unit or board deployment, so the first night on the deployed boxes is the real end-to-end check.
