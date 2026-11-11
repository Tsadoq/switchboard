# Commit-bound night installation artifacts

The night-artifacts workflow prepares both project wheels for root installs without
building source on an agent box. Adding this workflow does not authorize creating a
tag or release, root installation, test identities, enrollment or activation.

Publication is currently gated by review and convention, not an enforced human-only
identity boundary. A tag ruleset cannot distinguish a human from agents using the same
GitHub account/app credentials. Enforced separation would require an owner-authorized
restricted agent identity or approval credentials unavailable to agents. Do not create
identities, change repository permissions or add tokens as part of this source change.
No release proves that a human pressed the button. Installation still requires the
independently reviewed checksum pins in cluster-state and explicit run authorization.

By policy, only the human creates and pushes a publication tag after the source is
independently reviewed, CI is green and the exact commit is on main. This workflow
cannot enforce that actor distinction with the credentials currently shared.
The tag format is:

    night-artifacts-<full 40-character commit>

The workflow rejects a tag that does not name its checked-out commit or whose commit
is not an ancestor of origin/main. It runs lint, format, mypy, the full tests with an
isolated home, and byte-for-byte lock-export drift checks before building. The build
job has contents:read. Only its separate publishing job has contents:write. All four
actions are pinned to full commit SHAs, with version labels in their step names. The
release build imports no Actions cache, and checkout persists no credentials.

The backend uv-build==0.13.7 is a locked development dependency. uv is pinned to
0.12.5 in this workflow, the source commit timestamp is SOURCE_DATE_EPOCH, and both
wheels are built with --no-build-isolation --no-sources after installing the locked
environment. No latest compatible backend is resolved during the build. A dirty
package tree or preexisting dist directory is refused. BUILD_INFO.txt records the
actual Python, uv and backend versions; Python uses the supported 3.13 patch line.
Do not claim universal cross-platform bit reproducibility from a local repeat build.

The release contains:

- agent_board-0.1.0-py3-none-any.whl
- board-0.1.0-py3-none-any.whl
- night-verifier-requirements.txt
- night-smoke-requirements.txt
- uv.lock
- SOURCE_COMMIT
- BUILD_INFO.txt
- SHA256SUMS covering the preceding files

The publishing job rechecks the source commit and checksums and creates a release
for the existing exact tag. It never overwrites or clobbers an existing release.
No workflow is triggered by an ordinary PR or main push to publish these assets.

The managed role must pin the repository, exact tag/commit, the expected SHA256 of
SHA256SUMS itself, and the expected SHA256 of each downloaded wheel and manifest. SHA256SUMS is an integrity record, not a claim
that source review, authorization or acceptance happened. Check SOURCE_COMMIT too.
Install dependencies with --require-hashes --only-binary=:all: and project wheels
with --no-deps into fixed root-owned releases. Never substitute a source checkout,
editable install, mutable current symlink or an artifact from another commit.

This source change creates no tags or releases and uploads no assets. Until the
human explicitly performs publication and the role has the reviewed expected hashes,
both installation and smoke execution remain refused.
