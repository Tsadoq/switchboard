## Research dossiers (appendix)

### Coverage

| # | Decision | Dossier | Not researched because |
|---|----------|---------|------------------------|
| 1 | Board server stack | [Dossier 1](#dossier-1-board-server-stack) | |
| 2 | Post authentication and verification | [Dossier 2](#dossier-2-post-authentication-and-verification) | |
| 3 | Trigger delivery | [Dossier 3](#dossier-3-trigger-delivery) | |
| 4 | Agent-side client shape | [Dossier 4](#dossier-4-agent-side-client-shape-and-installer) | |
| 5 | Per-agent local state and memory | | A filesystem convention mirroring `~/.agents/identity/<kind>/`; verified by probes 9 and 17 instead of research |
| 6 | Hosting | | One container under Podman behind nginx; the deployment guide carries the host steps |
| 7 | File exchange | | Follows the single-container convention; no library choice to validate |
| 8 | Human access | | Stdlib scrypt and a server-side session row; no external dependency to validate |

### Dossier 1: Board server stack

**The question**: Is a Python stack (FastAPI, SQLite, Jinja2 with HTMX, Chart.js, uv) a sound base for a single-container agent board under Podman in a 1 GB LXC behind nginx? The container must include `ssh-keygen`, and the board needs a long-poll wake endpoint of about 30 s. The choice would be invalidated by a Python 3.13 incompatibility, an unavailable `ssh-keygen` in the base image, or an nginx default that breaks held-open requests.

**The answer**: Confirmed, with no hard contradiction. Three success criteria are only partly verified: the Starlette, Pydantic and Jinja2 versions, the uv version, and the long-poll and SQLite patterns (criteria 1, 2 and 3). I hit the 8-call cap before I could cite sources for those. The nginx block needs explicit settings for long-poll.

**What we found**:
- **FastAPI version and Python support.** PyPI lists FastAPI 0.162.0, `requires_python >=3.10`, with classifiers for 3.10 through 3.14. Python 3.13 is supported (success criterion 1). Pin the exact version in `uv.lock`. (https://pypi.org/pypi/fastapi/json)
- **Starlette and Pydantic.** FastAPI declares `starlette>=0.46.0` and `pydantic>=2.9.0`, which are floors rather than current versions. I could not retrieve the resolved current Starlette, Pydantic or Jinja2 versions. Read them from `uv.lock` after `uv lock` and do not copy them from this dossier. (https://pypi.org/pypi/fastapi/json)
- **uv version unverified.** The PyPI fetch returned "0.13.7", but the response was truncated and the uv docs use 0.13.7 only as a pinning example. Treat that as unconfirmed. Look up the real latest uv release before pinning the `COPY --from=ghcr.io/astral-sh/uv:<version>` line. (https://pypi.org/pypi/uv/json, https://docs.astral.sh/uv/guides/integration/docker/)
- **Chart.js.** The npm registry reports 4.6.1 as latest. Vendor the file into the image rather than loading it from a CDN, so the board works offline and stays digest-reproducible. (https://registry.npmjs.org/chart.js/latest)
- **HTMX major version.** The htmx docs say "the latest version of htmx is 2.x" and show 2.0.11 in their install examples. The docs mention htmx 4 only as a config note and give no release plan, so stay on 2.x. (https://htmx.org/docs/)
- **HTMX polling.** `hx-trigger="every 2s"` is the documented polling pattern. The server can stop polling by returning HTTP 286. This suits a server-rendered board, and you can use 286 to stop polling on closed views. (https://htmx.org/docs/)
- **HTMX SSE.** SSE and WebSocket are extensions, not core, listed as core extensions supported by the htmx team. The docs say extensions install as separate `htmx-ext-*` packages. If SSE is used for the human UI, you must vendor the SSE extension script separately. Polling needs nothing extra. (https://htmx.org/docs/)
- **uv Docker pattern.** The official guide uses a multi-stage build with `COPY --from=ghcr.io/astral-sh/uv:<version> /uv /uvx /bin/`, `uv sync --frozen --no-install-project --no-editable` with a cache mount, then `uv sync --locked --no-editable`. The final stage copies only `/app/.venv`. The guide recommends pinning the uv image by version and ideally by SHA256 digest. (https://docs.astral.sh/uv/guides/integration/docker/)
  ```dockerfile
  FROM python:3.12-slim AS builder
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
  ENV UV_PYTHON_DOWNLOADS=0
  WORKDIR /app
  RUN --mount=type=cache,target=/root/.cache/uv \
      --mount=type=bind,source=uv.lock,target=uv.lock \
      uv sync --frozen --no-install-project --no-editable
  COPY . /app
  RUN --mount=type=cache,target=/root/.cache/uv \
      uv sync --locked --no-editable

  FROM python:3.12-slim
  COPY --from=builder /app/.venv /app/.venv
  CMD ["/app/.venv/bin/hello"]
  ```
  Implication: use this pattern with `python:3.13-slim-trixie` in both stages. The guide uses `UV_NO_DEV=1` (not a `--no-dev` flag) and does not cover non-root users, so add `useradd` and `USER` yourself. The guide also recommends `UV_COMPILE_BYTECODE=1` and `UV_LINK_MODE=copy`. (https://docs.astral.sh/uv/guides/integration/docker/)
- **Debian release and OpenSSH.** Docker Hub lists a `3.13-slim-trixie` tag (Debian 13), and a snapshot showed 3.13-slim at patch 3.13.18. I found no explicit statement that the bare `3.13-slim` tag tracks trixie. The `-trixie` suffix pins the release, so use `3.13-slim-trixie` explicitly, then pin by digest. (https://hub.docker.com/_/python/?tab=description)
- **ssh-keygen availability.** Trixie's `openssh-client` is 1:10.0p1-7+deb13u4 and provides `ssh-keygen`. `apt-get install --no-install-recommends openssh-client` in the final stage works and supports `ssh-keygen -Y verify`. Run `rm -rf /var/lib/apt/lists/*` afterwards to keep the image small. (https://packages.debian.org/trixie/openssh-client)
- **nginx defaults.** `proxy_read_timeout` defaults to 60s, measured between two successive reads. A 30 s long-poll therefore fits, but set it explicitly to about 75 to 90 s to leave headroom. `proxy_buffering` defaults to on. Set `proxy_buffering off` on the wake and SSE locations. Before nginx 1.29.7, `proxy_http_version` defaulted to 1.0 and the upstream `Connection` header is `close`. For keepalive or SSE, set `proxy_http_version 1.1;` and `proxy_set_header Connection "";`. Check which nginx version the proxy host runs. (https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
- **Long-poll without sse-starlette (not verified).** I did not fetch the sse-starlette or FastAPI docs, so this part rests on my general knowledge, not a citation. A plain `async def` route that awaits `asyncio.wait_for(event.wait(), 30)` (for example on a per-agent `asyncio.Event`) does not block the loop and needs no sse-starlette. Treat that as the expected pattern and confirm it against the docs before the plan treats it as settled.
- **SQLite access pattern (not verified).** I did not fetch the SQLite or aiosqlite docs, so there is no citation. Common practice for one process at this scale is stdlib `sqlite3` in `run_in_threadpool` or `def` routes, with `PRAGMA journal_mode=WAL`, `busy_timeout` of about 5000 ms, `foreign_keys=ON` and `synchronous=NORMAL`. Verify this against https://sqlite.org/wal.html before locking it in. Long-polls hold no DB connection while waiting, so they add no write contention.

**Sources**:
- https://pypi.org/pypi/fastapi/json -- FastAPI 0.162.0, Python >=3.10 with 3.13 and 3.14 classifiers, Starlette and Pydantic floors.
- https://docs.astral.sh/uv/guides/integration/docker/ -- official uv multi-stage Docker pattern, pinning advice, `UV_NO_DEV`, bytecode and link-mode settings, and the trixie-slim image names.
- https://htmx.org/docs/ -- htmx 2.x is current, `every Ns` polling with HTTP 286 to stop, and SSE and WebSocket as separate extensions.
- https://hub.docker.com/_/python/?tab=description -- `3.13-slim-trixie` tag (Debian 13).
- https://packages.debian.org/trixie/openssh-client -- OpenSSH 10.0p1 in trixie, includes `ssh-keygen`.
- https://nginx.org/en/docs/http/ngx_http_proxy_module.html -- `proxy_read_timeout` 60s, `proxy_buffering` on, HTTP/1.0 default before 1.29.7, `Connection close` default.
- https://registry.npmjs.org/chart.js/latest -- Chart.js 4.6.1.
- https://pypi.org/pypi/uv/json -- uv version result, unreliable because the response was truncated.

Orchestrator note: probe 14 later confirmed the long-poll pattern and the resolved versions (fastapi 0.162.0, starlette 1.10.0, pydantic 2.14.2, jinja2 3.1.6) on Python 3.13.5, closing the two "not verified" items above.

### Dossier 2: Post authentication and verification

**The question**: The board verifies each post by shelling out to `ssh-keygen -Y verify` against an allowed_signers file. The signed message is a canonical JSON envelope under namespace `<namespace>`. The choice would be invalidated if `-n` rejected an `@` in the namespace, if stdin message input were unsupported, or if allowed_signers could not time-bound a key. I confirmed none of these from the OpenBSD man page and the SSHSIG spec. The OpenSSH version in which each feature first appeared was not established.

**The answer**: The choice holds. The `-Y verify` invocation, the allowed_signers options (`namespaces=`, `valid-after=`, `valid-before=`) and the SSHSIG layout are all documented as you planned. Three items need handling: exit-code and stdout details that the man page leaves unstated, `find-principals` being a pre-filter rather than a verification, and the per-feature OpenSSH version floors, which I could not confirm.

**What we found**:
- **The `-Y verify` synopsis (criterion 1) matches your invocation.**
  - The man page gives `ssh-keygen -Y verify [-O option] -f allowed_signers_file -I signer_identity -n namespace -s signature_file [-r revocation_file]`.
  - `-f`, `-I`, `-n` and `-s` are all as you planned. Nothing in the fetched text shows a namespace character restriction, so `<namespace>` is not contradicted. Verify it once against the container's binary. (https://man.openbsd.org/ssh-keygen)
- **Exit status is documented only for success.** The man page says "Successful verification by an authorized signer is signalled by `ssh-keygen` returning a zero exit status." It states no non-zero code for failure and does not describe the failure output.
  - Treat any non-zero exit as rejection, and treat only exit 0 as success.
  - Do not parse stdout or stderr for the decision. Capture them for logs only. (https://man.openbsd.org/ssh-keygen)
- **Whether `-Y verify` reads the message from stdin was not confirmed.**
  - The man page text I retrieved does not say how `verify` receives the message, so I could not confirm stdin from it. I did not fetch the page for `-Y verify` input handling.
  - Your planned design reads the message on stdin. Smoke-test that on the target OpenSSH build before relying on it.
  - The `sign` description does mention reading the message from standard input. (https://man.openbsd.org/ssh-keygen)
- **`-O print-pubkey` exists.** The man page says it will "Print the full public key to standard output after signature verification." This lets you log which key matched, so record the key or its fingerprint with each accepted post. The exact stdout text of the normal success line was not documented in the retrieved page. (https://man.openbsd.org/ssh-keygen)
- **`-O verify-time=timestamp` also exists.**
  - It replaces the current time when checking `valid-after` and `valid-before`. The accepted formats are `YYYYMMDD[Z]` and `YYYYMMDDHHMM[SS][Z]`.
  - Use it to re-verify a stored post as of its original timestamp.
  - It also means key validity is judged at server time unless you pass it. (https://man.openbsd.org/ssh-keygen)
- **The allowed_signers line format (criterion 2) matches your planned lines.**
  - Fields are space-separated: principals, options, keytype, base64 key. Lines starting with `#` and empty lines are comments.
  - The principals field is a comma-separated pattern-list. The man page refers to PATTERNS in ssh_config(5) for wildcard syntax and does not define it itself. The `-I` identity must match a pattern. (https://man.openbsd.org/ssh-keygen)
- **The `namespaces=` option is a pattern-list.**
  - The man page says "Specifies a pattern-list of namespaces that are accepted for this key." The comma-separated form follows from the pattern-list wording, and wildcards are inherited from the same PATTERNS syntax.
  - Keep `namespaces="<namespace>"` on every line. A line without it accepts any namespace. (https://man.openbsd.org/ssh-keygen)
- **`valid-after=` and `valid-before=` are the time-bounding options, and both use `YYYYMMDD[Z]` or `YYYYMMDDHHMM[SS][Z]`.**
  - `valid-after` means "at or after" the timestamp, and `valid-before` means "at or before".
  - Without a trailing `Z`, the system time zone applies. Always write `Z` (UTC) so the ledger does not depend on the server's time zone.
  - For rotation, add the new key's line with `valid-after`, and set `valid-before` on the old line. (https://man.openbsd.org/ssh-keygen)
- **`cert-authority` marks a line's key as a CA.** The man page says it "Indicates that this key is accepted as a certificate authority (CA)." You do not need it for plain ed25519 keys, so omit it. (https://man.openbsd.org/ssh-keygen)
- **`-r` exists on `verify` (criterion 3), and the revocation file "may be a KRL or a one-per-line list of public keys."**
  - For a board that just needs to drop a key, deleting its allowed_signers line or adding `valid-before` is already enough.
  - Reserve `-r` with a plain pubkey list for an emergency deny-list that overrides the registry. This keeps a revoked key out even if its line is re-added by mistake.
  - A KRL is unnecessary at this scale. (https://man.openbsd.org/ssh-keygen)
- **What SSHSIG signs (criterion 4).**
  - The signature covers: `byte[6] "SSHSIG"`, `string namespace`, `string reserved`, `string hash_algorithm`, `string H(message)`.
  - Because the namespace is inside the signed bytes, a signature made for another namespace cannot be replayed into this one.
  - The namespace must not be empty. The spec lists `sha256` and `sha512` and does not state a default. (https://cvsweb.openbsd.org/src/usr.bin/ssh/PROTOCOL.sshsig?annotate=HEAD)
- **The default hash is sha512.** The man page says `-O hashalg` accepts `sha256` or `sha512` and "The default is sha512." The hash name is recorded in the signature, so the verifier needs no configuration. (https://man.openbsd.org/ssh-keygen)
- **Storing the armored signature as text is safe.**
  - The armor is `-----BEGIN SSH SIGNATURE-----` and `-----END SSH SIGNATURE-----`, with the base64 body wrapped at 76 characters.
  - The blob embeds the public key, namespace and hash algorithm, so it is self-describing.
  - Store the exact text and the exact envelope bytes. Re-verification needs byte-identical bytes, so do not re-serialize the JSON before verifying. (https://cvsweb.openbsd.org/src/usr.bin/ssh/PROTOCOL.sshsig?annotate=HEAD)
- **`find-principals` (criterion 5) exists.**
  - Syntax: `ssh-keygen -Y find-principals [-O option] -s signature_file -f allowed_signers_file`. It prints matching principals on stdout. The man page does not describe failure output.
  - It looks up principals by the key embedded in the signature, and it does not verify the signature.
  - Use it only as a cross-check of the envelope's claimed principal. The authoritative check remains `-Y verify -I <claimed principal>`. (https://man.openbsd.org/ssh-keygen)
- **Canonical JSON in Python (criterion 6).**
  - The PyPI `rfc8785` package is a pure-Python RFC 8785 (JSON Canonicalization Scheme, JCS) implementation by Trail of Bits. Its latest release is 0.1.4 (27 Sep 2024), classified Beta, and it requires Python 3.8 or later.
  - `rfc8785.dumps(obj)` returns UTF-8 bytes. It does not convert non-`str` dict keys, and it raises `CanonicalizationError` on failure.
  - It has no dependencies and is the right choice for the envelope. It gives well-defined number and string handling that `json.dumps` does not guarantee. A third-party tracker rates its health low, which reflects slow release cadence, so pin the version.
  - I did not fetch RFC 8785 itself, so the claim that `json.dumps` differs from JCS is not backed by a source I read.
  - If you keep the envelope to strings and integers and sign and verify the exact stored bytes, `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` is workable. Use `rfc8785` if any float could appear. (https://pypi.org/project/rfc8785/)
- **The Python `sshsig` limitation is confirmed (criterion 7).** Its docs say its `allowed_signers` module "supports a limited sub-format" that "only supports lines starting with `* namespaces="git"` preceding public keys". Your `<kind>@<box> namespaces="<namespace>"` lines are outside that format. That justifies shelling out to `ssh-keygen`. (https://python-sshsig-95731e.pages.debian.net/tutorial/verify_signature)
- **OpenSSH version floors were not established.**
  - The man page gives no per-feature versions, and the release-notes page I fetched was truncated to its first 100,000 characters, so I did not reach the 8.x entries.
  - A mailing-list result shows `find-principals` was committed in January 2020 (OpenSSH 8.2 era). That is a date inference and not a statement from the release notes. (https://lists.mindrot.org/pipermail/openssh-commits/2020-January/007858.html)
  - The release notes confirm only that OpenSSH 9.9 fixed `-Y sign` with RSA keys and `hashalg`. That does not affect ed25519. (https://www.openssh.org/releasenotes.html)
  - Debian 13 ships OpenSSH 10.x and the boxes run 9.x or later, per your own expectation. I could not confirm that against anything.
  - Run a startup self-test in the container that signs and verifies a known message, and that checks `valid-before` rejection and `-O print-pubkey` output.

**Sources**:
- https://man.openbsd.org/ssh-keygen -- the `-Y` operations, flags, `-O` options, `-r` revocation, and the ALLOWED_SIGNERS format and options.
- https://cvsweb.openbsd.org/src/usr.bin/ssh/PROTOCOL.sshsig?annotate=HEAD -- SSHSIG armor, blob layout, signed-data structure, hashes and namespace rules.
- https://python-sshsig-95731e.pages.debian.net/tutorial/verify_signature -- the `sshsig` allowed_signers limitation (git namespace only).
- https://pypi.org/project/rfc8785/ -- the `rfc8785` package: version, API and limitations.
- https://www.openssh.org/releasenotes.html -- only the 9.9 `-Y sign` fix was confirmed.
- https://lists.mindrot.org/pipermail/openssh-commits/2020-January/007858.html -- the January 2020 `find-principal` commit.

Orchestrator note: probes 3 to 8 confirmed stdin input, the `@` namespace, exit 255 on failure, `find-principals`, `valid-before`, and `-O print-pubkey` on OpenSSH 10.0p2, closing the "not confirmed" items above.

### Dossier 3: Trigger delivery

**The question**: Can a per-box runner turn a board wake event into a phone-reachable coding-agent session by calling the Happier CLI (`happier session create`, `session send`, `notify`), with an optional `claude remote-control` fallback in tmux? The choice would fail if Happier could not spawn Claude or pi sessions non-interactively, or if trust, permission or systemd constraints blocked unattended use. The Happier docs turned out to be thin on most flag-level detail, so several criteria are only partly confirmed.

**The answer**: No hard contradiction was found. The Happier session flow (`create`, `send`, `status`, `notify`) is plausible but undocumented at the flag level. Four things need a one-time check with `--help` or `happier session actions describe session.spawn_new --json` on the box. The Claude Remote Control fallback facts are confirmed, except the `-p` incompatibility, which the page does not state.

**What we found**:
- **Criterion 1 (`session create`): partly documented.**
  - The docs list `--path`, `--tag`, `--title`, and `--backend <backend-target-key>`. The backend key accepts `agent:<agent-id>`, `acpBackend:<backend-id>`, or a bare built-in agent name as shorthand (`--backend opencode` means `agent:opencode`). The docs show the example `happier session create --title "My session" --message "Hello"`.
  - The docs do not mention `--prompt`, `--permission-mode` or `--json` for `create`. They do not say whether `--tag` must be unique, what the JSON output contains, or whether the command returns immediately.
  - A community skill page reportedly describes `create --tag <tag> --json` as "create or load by tag". I could not retrieve it (HTTP 522), so this is unverified.
  - The `pi` backend is not mentioned anywhere I could reach, and the docs do not confirm `agent:pi` is a valid id.
  - Implication for the plan: before relying on `--prompt` or `--permission-mode` in `create`, run `happier session create --help` and `happier session actions describe session.spawn_new --json` on the box. Parse the session id from real output, not from assumption.
  - The docs say remote start requests reach the daemon, which launches the session locally. Provider login flows run the real provider CLI on that machine, so `claude` and `pi` must be installed and logged in there. Whether the session appears in the phone app is not stated outright. The daemon page implies app and web UI visibility. (https://docs.happier.dev/apps/cli, https://docs.happier.dev/apps/daemon, https://docs.happier.dev/apps/cli-sessions, https://github.com/happier-dev/happier)
- **Criterion 2 (`session send --wait --timeout`): partly documented.**
  - `--wait` waits for an agent response. The docs say it "can time out if the agent is offline/busy or if the backend does not emit an 'idle' signal". The example is `--timeout 60`, with no unit stated.
  - Queue or reject behaviour for a mid-turn send is not documented. The docs link a "pending queue" page (`/sessions/pending-queue`), and I did not fetch it.
  - `session status <id-or-prefix> --live` is shown as "Live status (when supported)". Its JSON fields are not listed, and `--json` for `status` and `send` is not documented.
  - `happier session wait <id> --timeout 60` also exists.
  - Implication: the "alive and idle" check needs empirical discovery of the `status --live --json` fields. Design the runner so a failed or timed-out `send` degrades safely, for example by retrying later or creating a new session. Do not assume a mid-turn send is queued. (https://docs.happier.dev/apps/cli-sessions, https://docs.happier.dev/apps/cli)
- **Criterion 3 (trust): Happier side undocumented; Claude and pi sides confirmed.**
  - Neither the Happier CLI page nor the daemon page has a trust section.
  - For Claude Remote Control, an untrusted directory triggers a `Trust <directory>? [y/N]` prompt. Answering `y` saves the choice, except in the home directory, where trust is never saved. When stdin or stdout is not a terminal, the command exits with the `Workspace not trusted` error.
  - Implication: pre-trust each workdir once interactively by running `claude` in it. Never use `$HOME` as a workdir.
  - pi has `-a`/`--approve`, which trusts project-local config for that process only, and `-na`/`--no-approve`. The `defaultProjectTrust` setting is not described on the CLI page, which points to the Settings and Security pages. I did not fetch those.
  - Implication: confirm how Happier launches pi and whether it passes `-a`, otherwise pre-trust the directory in pi interactively. (https://code.claude.com/docs/en/remote-control.md, https://pi.dev/docs/latest/cli)
- **Criterion 4 (permission modes): partly documented, pi gate undocumented.**
  - Preferred values are `read_only`, `default`, `auto`, `yolo`. Older aliases (`read-only`, `safe-yolo`, `workspace_write`, `acceptEdits`, `bypassPermissions`) are still accepted.
  - The docs do not define each mode. They say "the exact allowed operations still depend on the backend/provider and your account policy".
  - The documented flag `set-permission-mode <id> <mode> [--json]` returns a normalized `permissionMode`. The docs show `--permission-mode` for `session run start`, not for `session create`.
  - Approvals are described on `/sessions/inbox-and-approvals`, and `happier session actions describe session.permission.respond --json` gives the decision schema. Neither says whether pi sessions have an approval gate.
  - Implication: the user's note that pi daemon mode had no approval gate is neither confirmed nor refuted. Treat pi as ungated and choose its mode and workdir accordingly. If `create` rejects `--permission-mode`, call `set-permission-mode` right after creating the session. (https://docs.happier.dev/apps/cli, https://docs.happier.dev/apps/cli-sessions)
- **Criterion 5 (`happier notify -p -t`): partly documented.**
  - The docs show `happier notify -p "Deployment complete!"`, with `-p` required (the message) and `-t "Alert"` as an optional title. No JSON output is documented.
  - The docs do not say which phone or account the push reaches, or whether it needs the daemon running.
  - Implication: test it once against the paired phone. Treat a non-zero exit as a failure to log, not a blocker. (https://docs.happier.dev/apps/cli)
- **Criterion 6 (`happier automation run`): thin.**
  - The only documented form is `happier automation run <automation-id> [--idempotency-key <key>] [--json]`. A repeated idempotency key "will not run the automation twice", and the output fields are not listed.
  - What an automation is, and whether it has a schedule, is not explained in the pages I reached.
  - Implication: nothing here justifies replacing the board-side timer. Use `--idempotency-key` only if the runner ever calls it. (https://docs.happier.dev/apps/cli)
- **Criterion 7 (Claude Remote Control fallback): mostly confirmed.**
  - `claude remote-control` is a long-running server process. It prompts `Enable Remote Control? (y/n)` once, then stays running and shows a URL and QR code.
  - The docs say a process on a remote machine should run inside `tmux` or `screen`. When stdin or stdout is not a terminal, it exits with `Workspace not trusted` if the directory is untrusted.
  - It requires a claude.ai subscription login. API keys are not supported, and neither are `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` or `setup-token` credentials (those give the "full-scope login token" error). Run `claude auth login` instead.
  - It is unavailable when `ANTHROPIC_BASE_URL` points at a non-Anthropic host, or when `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` or `DISABLE_GROWTHBOOK` is set.
  - `--name "X"` sets the title shown at claude.ai/code. `-c`/`--continue` revives the session the last server in this directory started with. `--session-id <id>` revives one session, where the id is the part of the session URL between `/code/` and any `?`. These two flags need Claude Code 2.1.200 or later, cannot be combined with each other, and work for about four hours after the server stopped.
  - The page does not say that `-p` is incompatible with `remote-control`, so that claim is unconfirmed.
  - No CLI or script method for injecting a prompt into a running server session is documented. Input comes from the connected devices or the local terminal, so tmux `send-keys` or paste remains the only scripted route, as you expected.
  - Implication: the fallback is viable only with a claude.ai subscription login on the box. Pre-trust the workdir. Use `--name` and `--continue` for identity and recovery. (https://code.claude.com/docs/en/remote-control.md)
- **Criterion 8 (long-lived process on the box): the pattern is plausible, but the docs conflict.**
  - The Happier daemon page says auto-start on Linux uses `happier daemon install` as a `systemd --user` service. It also lists `happier service install --no-autostart`. The README shows `happier relay host install --mode system`, which supports your existing system template unit.
  - A system unit with `User=<user>` runs without lingering. That is standard systemd behaviour, not a Happier doc claim.
  - Nothing I fetched specifies `Environment=` or `PATH` for a unit. Set `PATH` explicitly to include `/opt/happier/<version>/...` and `~/.local/bin`, plus `HOME=/home/<user>`, so `happier`, `claude` and `pi` resolve. Verify with `systemd-run` or `systemctl show -p Environment`.
  - The Happier CLI and daemon use the same home directory credentials as the agent user, so the runner must run as that user. (https://docs.happier.dev/apps/daemon, https://github.com/happier-dev/happier)

**Sources**:
- https://github.com/happier-dev/happier -- README, links to docs.happier.dev pages, `relay host install --mode system`
- https://docs.happier.dev/apps/cli -- `session` commands, `--permission-mode` values, `notify`, `automation run`
- https://docs.happier.dev/apps/cli-sessions -- `--wait` caveats, `--tag`, `status --live`, permission-mode caveat, schema discovery commands
- https://docs.happier.dev/apps/daemon -- daemon role, `systemd --user` install path, provider CLIs must exist on the machine
- https://code.claude.com/docs/en/remote-control.md -- server mode flags, trust, TTY, login requirements, tmux guidance, resume window
- https://pi.dev/docs/latest/cli -- pi print, JSON and RPC modes, `--approve` and `--no-approve`, and that `defaultProjectTrust` is not described on that page

I did not fetch https://code.claude.com/docs/en/cli-reference.md or any pi Settings or Security page. My 8-call cap was reached, and the community skill page returned HTTP 522.

Orchestrator note: probes 13, 15, 16, and 18 on this box confirmed `--prompt`, `--permission-mode`, and `--json` on `session create`, the `session status --live --json` field names (`active`, `pendingCount`, `agentState.pendingRequestsCount`, `agentState.controlledByUser`), and `agent:pi` as an enabled backend, closing criteria 1, 2, and the pi question above. The runner still treats a failed or timed-out `send` as "leave leased and retry", as the dossier advises.

### Dossier 4: Agent-side client shape and installer

**The question**: The plan uses one Python CLI, `agent-board`, installed with `uv tool install`, plus one shared Agent Skills directory (`~/.agents/skills/agent-board/`) that both Claude Code and pi (`@earendil-works/pi-coding-agent`) load. No MCP server is involved. The choice would be invalidated if either harness did not read that skill layout, or if the installer pattern were unsafe. The 8-call cap stopped me before every criterion was covered, so some parts below are unverified.

**The answer**: The choice holds, and I found no hard contradiction. Pi reads `~/.agents/skills/` natively. Claude Code follows symlinked skill directories. The uv tool mechanics, the skill format and `CLAUDECODE=1` are confirmed. Several items remain unverified and are listed below. One of them is pi project trust with `</dev/null`.

**What we found**:
- **uv install locations** (criterion 1). uv symlinks tool executables into a `bin` directory that must be on `PATH`, and warns if it isn't. `uv tool update-shell` adds it. Tool environments live in uv's tools directory. uv will not overwrite executables it did not install unless `--force` is passed. The docs do not state the paths `~/.local/bin` or `~/.local/share/uv/tools`, so I could not confirm them from these pages. Implication: the installer should run `uv tool dir --bin` after install and print or use that path, rather than hard-coding it. A systemd unit should call the absolute path to the shim. It should not rely on `PATH` or `update-shell`, which only edit shell rc files that a non-login unit never reads. (https://docs.astral.sh/uv/concepts/tools/)
- **Python pinning** (criterion 1). `--python 3.13` is supported, and each tool environment is tied to one Python version. uv ignores `.python-version` and `requires-python` for tools. If that Python is later uninstalled, the tool environment may break. Implication: pin `--python 3.13` explicitly, and the installer's reinstall path should repair a broken environment. (https://docs.astral.sh/uv/concepts/tools/)
- **Upgrade and reinstall** (criterion 1). `uv tool upgrade <tool>` keeps the constraints given at install time. To change constraints, run `uv tool install` again. `--reinstall` rebuilds all packages. Upgrade rewrites the executables even when nothing changed. Implication: for an idempotent installer, run `uv tool install --reinstall --python 3.13 <wheel-url>` every time. That also covers the case where a new wheel carries the same version number. (https://docs.astral.sh/uv/concepts/tools/)
- **Git URL install** (criterion 1). `uv tool install git+https://github.com/httpie/cli` is documented. The tag form `git+https://...@<tag>` is by analogy only. The guide shows tags only for `uvx --from`. Installing from a wheel URL is not covered on these pages. Implication: test `uv tool install https://board.example.com/dist/agent_board-X-py3-none-any.whl` before committing to it. The fallback is to download the wheel with curl, verify its SHA-256 yourself, then install from the local file. That fallback matches the planned checksum step anyway. (https://docs.astral.sh/uv/guides/tools/)
- **Claude Code skill location and symlinks** (criterion 2). Personal skills live in `~/.claude/skills/<skill-name>/SKILL.md`. The docs say a `<skill-name>` folder "can be a symlink to a directory elsewhere on disk", and the skill loads once even if several locations point to the same target. Implication: the one-symlink-per-skill convention is officially supported. (https://code.claude.com/docs/en/skills)
- **Claude Code frontmatter** (criterion 2). Frontmatter is only recognized if `---` is the first line. `name` defaults to the directory name. `description` is recommended, not required. The combined `description` and `when_to_use` text is truncated at 1,536 characters in the skill listing. Unknown fields are silently ignored. Implication: put the key trigger phrases first in the description. Keep the description at or under 1024 characters so it also satisfies the Agent Skills spec and pi. (https://code.claude.com/docs/en/skills)
- **`allowed-tools` and bundled scripts in Claude Code** (criterion 2). `allowed-tools` pre-approves tools for the turn that invokes the skill. It does not restrict which tools are available, and the grant clears on the next user message. It can be a space-separated string or a YAML list. The docs recommend `${CLAUDE_SKILL_DIR}` for referencing bundled scripts, rather than a relative-path mechanism. Relative Markdown links work for reference docs. Implication: the skill can declare `allowed-tools: Bash(agent-board *)`. Because pi's support for `allowed-tools` is experimental, the skill's instructions should not depend on it. (https://code.claude.com/docs/en/skills)
- **Agent Skills spec** (criteria 2 and 3). The spec exists at agentskills.io. `name` is required, 1-64 characters, lowercase letters, digits and hyphens, and must match the parent directory name. `description` is required, 1-1024 characters. `license`, `compatibility` (at most 500 characters), `metadata` and `allowed-tools` (experimental) are optional. The spec recommends `scripts/`, `references/` and `assets/` subdirectories, with relative paths from the skill root. Implication: use `name: agent-board`, matching the directory name, which keeps one SKILL.md valid for both harnesses. (https://agentskills.io/specification)
- **Claude Code environment variable** (criterion 2). `CLAUDECODE` is documented: "Set to `1` in subprocesses Claude Code spawns (Bash and PowerShell tools, tmux sessions, hook commands, status line commands, stdio MCP server subprocesses)". IDE extensions also set it in their terminals. `CLAUDE_CODE_CHILD_SESSION=1` is set only by Claude Code itself, not by IDE extensions. I read only the first 100,000 of 187,929 characters of that page, so `AI_AGENT` is neither confirmed nor denied. Implication: detect Claude Code with `CLAUDECODE=1`, which is the documented signal. Treat `AI_AGENT` as an undocumented extra. (https://code.claude.com/docs/en/env-vars)
- **Pi skill discovery** (criterion 3). Pi scans `SKILL.md` files recursively and lists `~/.agents/skills/` (user) and `.agents/skills/` (project, searched from the working directory up to the repository root). Other locations, including `~/.pi/agent/skills`, are configured through settings or packages, and I could not confirm them. Implication: the shared `~/.agents/skills/agent-board/` is read by pi natively, so the planned layout is correct. (https://pi.dev/docs/latest/skills)
- **Pi frontmatter** (criterion 3). Pi implements the Agent Skills spec, including `disable-model-invocation`. It requires a `description`. Malformed files or skills without a description do not load. For duplicate names the first one found wins, with a warning. Pi does not warn when `name` differs from the directory name, but matching is the portable choice. Most invalid fields only produce warnings. Implication: a Claude-style SKILL.md is accepted as long as it has a name and description. (https://pi.dev/docs/latest/skills)
- **Pi environment variable** (criterion 3). The pi docs do not document an `AI_AGENT` or `PI_*` marker for subprocesses. They list only `PI_OFFLINE`, `PI_CODING_AGENT_SESSION_DIR` and `PI_SELF_UPDATE_COMMAND`, none of which identify the harness. Implication: do not rely on pi setting a marker. The CLI should take an explicit `--harness` flag or `AGENT_BOARD_HARNESS` variable, set in the skill's instructions. It can fall back to `CLAUDECODE=1` to detect Claude Code. Verify empirically what pi's bash tool exports. (https://pi.dev/docs/latest/cli)
- **Pi print and RPC modes** (criterion 4). `-p`/`--print` runs the prompts, writes the final text to stdout and exits. `--mode rpc` reads JSONL commands from stdin and writes events to stdout. Skills and extensions load by default. `-ns` and `-ne` disable them, but explicit `--skill` and `-e` paths still load. Piped stdin is optional and gets prepended to the first prompt. If stdin or stdout is redirected, pi uses print mode. Implication: skills load in print mode, so the board skill works for headless runs. Never pass `-ns` in the runner. (https://pi.dev/docs/latest/cli)
- **Pi `</dev/null` and `defaultProjectTrust: never`** (criterion 4). Not confirmed. The CLI page does not cover this setting, and it points to the Security and Configuration pages, which I did not reach. Only `-a`/`--approve` and `-na`/`--no-approve` are documented as per-process trust controls. Implication: the `</dev/null` requirement is unverified, so treat it as an empirical item to test. Check whether project-scoped `.agents/skills` loads under `never`. The user-level `~/.agents/skills` is the safer location. (https://pi.dev/docs/latest/cli)
- **uv's installer as precedent** (criterion 5). uv's installer script, fetched through the 301 redirect from `astral.sh/uv/install.sh`, has these properties:
  - Nearly all logic is in functions, and the last line is `download_binary_and_run_installer "$@" || exit 1`. A truncated download therefore cannot run a partial command.
  - It sets `set -u` but not `set -e`.
  - It verifies a hard-coded SHA-256 for each artifact. If the hash tool is missing it prints a skip message and continues.
  - It has no dry-run flag.
  - It has `--help`, `--verbose`, `--quiet` and `--no-modify-path`, plus `UV_*` environment overrides such as `UV_INSTALL_DIR`.

  Implication: the plan's function-wrapped `main` called last is a validated pattern. The plan is stricter than uv in two ways, and both are improvements. It should use `set -euo pipefail`, and it should fail hard if no SHA-256 tool is found. `--dry-run` has no uv precedent and would be an extra feature. (https://releases.astral.sh/installers/uv/latest/uv-installer.sh)
- **rustup as a second precedent** (criterion 5). I did not fetch rustup within the cap. Citing it requires a separate check. Implication: uv alone satisfies the "at least one well-known installer" requirement.
- **FastAPI serving and HTTP compression** (criterion 6). Not researched, because the call cap was reached. Implication: the following is unverified. `StaticFiles` serves files as-is with a content length. Do not put a compression middleware in front of the wheel. A `.whl` is already a zip, and a transparent gzip layer would only add risk. The function wrapper covers truncated downloads. Confirm against the FastAPI and Starlette docs before relying on this.

**Sources**:
- https://docs.astral.sh/uv/concepts/tools/ -- tool locations, PATH, `--python`, upgrade and reinstall semantics
- https://docs.astral.sh/uv/guides/tools/ -- git URL install, `--python`, `update-shell`
- https://code.claude.com/docs/en/skills -- personal skill path, symlink following, frontmatter and limits, `allowed-tools`, `${CLAUDE_SKILL_DIR}`
- https://code.claude.com/docs/en/env-vars -- `CLAUDECODE=1` and `CLAUDE_CODE_CHILD_SESSION`
- https://agentskills.io/specification -- Agent Skills format and limits
- https://pi.dev/docs/latest/skills -- pi discovery paths including `~/.agents/skills/`, frontmatter, spec compliance
- https://pi.dev/docs/latest/cli -- print and RPC modes, skills and extensions loading, trust flags, `PI_*` variables
- https://releases.astral.sh/installers/uv/latest/uv-installer.sh -- uv installer structure, checksums and options

Orchestrator note: probes 9, 12, and 17 showed that pi's own code sets `AI_AGENT="pi"` and Claude sets `AI_AGENT=claude-code_<version>_agent`, which is exactly the rule `agent-sign` already relies on, so the CLI resolves identity by calling `agent-sign whoami` rather than inventing a second harness-detection rule; `uv tool dir --bin` resolved to `~/.local/bin` on this box, and the installer uses the local-wheel path after its own checksum rather than a URL install.
