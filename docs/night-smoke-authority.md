# Isolated enforcer smoke authority

This fixture is not a production acceptance grant or permission to run a root test.
It is source for a separately authorized managed enforcer smoke. It does not prove
native launcher authentication, repository isolation, production window policy or
fleet admission behavior. The production pilot remains off.

## Installed artifacts

Use a fixed, root-owned release and interpreter, never a checkout or editable venv.
The smoke environment needs both reviewed board and agent-board wheels, each
SHA256-checked and installed with --no-deps. Install the dependencies from
`deploy/night-smoke-requirements.txt` with --require-hashes --only-binary=:all:.
Regenerate the artifact without resolution changes using:

    uv export --all-packages --no-dev --no-emit-workspace --format requirements-txt --no-header --no-annotate --locked --output-file deploy/night-smoke-requirements.txt

This larger closure is for smoke only; it does not change the normal verifier
release dependency contract.

## Protected fixture configuration

Create a new root0700 private directory for each deliberate test. authority.json
must be root0600, have no symlinked/writable ancestors, and contain exactly:

- box: configured non-operational host name.
- principal: an allowed coding principal for that box, not the verifier service.
- starts_at: a finite UTC epoch fixed for this one test, set after installation.
- duration_seconds: an integer from 1 to 120, never renewed.
- tls_cert, tls_key, verifier_public_key: normalized absolute files directly inside
  the fixture directory. The private TLS key is root0600; public files are root-owned
  and not group/other writable.
- port: a fixed free TCP port from 1 to 65535, listening only on 127.0.0.1.

verifier_public_key contains exactly one allowed-signers line for the separate
fixture verifier key, namespace switchboard, key type ssh-ed25519. Do not copy any
production private identity into the fixture or register the fixture key in the
production board. The fixture creates its own role file and signer registry.

The managed playbook must independently pin the verifier's launcher to
agent_board.night.acceptance_stub. This authority cannot attest host configuration
from a five-field grant. The test verifier, guard identity, socket, journal and
ledger must be separate from production, with physical slice emptiness checked
before the smoke. The static production guard must not be started or overridden.

## Two-step lifecycle

First run, as root under an explicitly authorized managed transient unit:

    <root smoke python> -I -m board.night_smoke_authority serve --config <private directory>/authority.json

The fixture uses the actual board application, SSH verifier, role checks, nonce
handling, private grant routes and receipt accounting. It ignores production
BOARD_* environment settings. It enables no production identity or enrollment.
It refuses an existing fixture database rather than resetting its history. Its
HTTPS certificate must be the explicit ca_path in the protected smoke verifier
configuration. Web login is locked behind a generated, undisclosed random password.

Start and reconcile the smoke verifier against this initially empty authority.
Only after its journal UUID has been bootstrapped, run:

    <root smoke python> -I -m board.night_smoke_authority admit --config <private directory>/authority.json

This admits one run in the throwaway database and writes root0600 run.json with
run_id, fence and board_url. It refuses admission before bootstrap, after expiry or
after any prior run. Pass the fence to the unprivileged request client on stdin,
never argv or logs. The test principal must map to the caller's real configured UID.

The fixture process explicitly replaces its own calendar policy with the one short
configured interval. This affects only that isolated process and database. Ordinary
board startup imports no fixture code and keeps the Europe/Rome night policy.
The fixture's enrollment and control flag belong only to the temporary database;
production control.enabled must remain unchanged.

After kernel evidence, authenticated settlement and local slot release have been
recorded, stop the smoke services and remove their private artifacts. Never remove
state merely because a timeout expired or a process reported idle. Keep the smoke
execution gate closed until source review, pinned release/dependency artifacts and
explicit human authorization for that individual managed run are all present.
