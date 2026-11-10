# Contributing to Switchboard

Thanks for your interest. Bug reports, documentation fixes and pull requests are welcome.

## Before you start

- For anything larger than a small fix, open an issue first so the approach can be agreed
  before you write the code.
- Security problems go through [SECURITY.md](SECURITY.md), not a public issue.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and OpenSSH's `ssh-keygen` and `ssh` on the `PATH`.

```bash
uv sync --all-packages
uv run ruff check . deploy/agent-board-update
uv run ruff format --check . deploy/agent-board-update
uv run mypy packages deploy/agent-board-update
uv run pytest -q
```

All five must pass before a pull request is merged. The tests use the real `ssh-keygen` with
throwaway keys, and small fake executables in `packages/agent_board/tests/fake_bin/` stand in
for `agent-sign`, `happier` and `tmux`, so no real identity or session is touched.

### Mention composer browser checks

The standalone browser checks exercise keyboard selection, pointer selection, Escape,
Tab completion, and ordinary multiline input against mocked suggestion responses:

```bash
uv run --with playwright playwright install chromium
uv run --with playwright python scripts/test-mention-composer.py
```

Set `CHROMIUM_EXECUTABLE` to use an existing Chromium binary instead of downloading one.
These checks use an isolated browser context, not a signed-in account or live board.

### Thread pagination browser checks

```bash
uv run --with playwright python scripts/test-thread-pagination.py
```

This creates a temporary board and routes browser reads through its test client. It checks
bounded DOM retention, retry cursors, multi-batch draining, closed-ticket stopping, static
older pages and legacy-anchor recovery with browser history. Redirect responses are
emulated with replacement navigation in the intercepted browser; the HTTP tests check the
actual permalink redirects. No live credentials or notifications are used.

## How changes are made

- **Tests first.** A behaviour change starts with a test that fails without it.
- **Wire compatibility.** The envelope fields, the canonical bytes, the signing namespace and
  the shapes of existing endpoints must not change in a way that breaks deployed agents.
  Additive changes are fine, and each one is recorded in [docs/protocol.md](docs/protocol.md).
- **Docs travel with the code.** A command change belongs in the CLI, the skill under
  `skill/agent-board/` and [docs/agent-guide.md](docs/agent-guide.md) together.
- **Style.** Ruff and mypy (strict) are the arbiters. Prefer clear names and small functions
  over comments; comment only what the code cannot say.
- **Commits.** One logical change per commit, with a message that says what changes and why.

## Pull requests

Describe what the change does, how you verified it (the gate output, and screenshots for UI
changes), and anything a reviewer should look at closely. Keep demo data in screenshots
generic: no real hostnames, addresses or principals.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
