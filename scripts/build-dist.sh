#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKSUMS="SHA256SUMS"
INSTALLER_DIR="installer"
SKILL_DIR="skill/agent-board"

staging=""

usage() {
	cat <<EOF
Usage: build-dist.sh [OUT_DIR]

Builds everything the board serves under /dist: the agent-board wheel, every file in
$INSTALLER_DIR/, the skill under $SKILL_DIR/, and a $CHECKSUMS manifest the installer
verifies against. OUT_DIR (default: $REPO/dist) is replaced as a whole, and only when
it is empty or holds an earlier build.
EOF
}

die() {
	printf 'build-dist.sh: %s\n' "$*" >&2
	exit 1
}

check_replaceable() {
	local out="$1"
	if [[ -e $out && ! -f $out/$CHECKSUMS && -n "$(ls -A -- "$out")" ]]; then
		die "refusing to replace $out: it is not empty and holds no $CHECKSUMS from an earlier build"
	fi
}

populate() {
	local staging="$1" wheels="$1/.wheels"
	(cd -- "$REPO" && uv build --package agent-board --wheel -o "$wheels")
	mv -- "$wheels"/*.whl "$staging/"
	rm -rf -- "$wheels"
	cp -- "$REPO/$INSTALLER_DIR"/* "$staging/"
	mkdir -p -- "$staging/${SKILL_DIR%/*}"
	cp -R -- "$REPO/$SKILL_DIR" "$staging/$SKILL_DIR"
}

write_checksums() {
	(
		cd -- "$1"
		find . -type f ! -path "./$CHECKSUMS" -printf '%P\0' | LC_ALL=C sort -z | xargs -0 sha256sum >"$CHECKSUMS"
	)
}

main() {
	if [[ ${1:-} == -h || ${1:-} == --help ]]; then
		usage
		exit 0
	fi
	(($# <= 1)) || die "expected at most one OUT_DIR argument"
	local out
	out="$(realpath -m -- "${1:-$REPO/dist}")"
	check_replaceable "$out"
	mkdir -p -- "${out%/*}"
	staging="$(mktemp -d -- "$out.XXXXXX")"
	trap 'rm -rf -- "$staging"' EXIT
	# mktemp -d creates the directory 0700, and the board serving it may run as another user.
	chmod 0755 -- "$staging"
	populate "$staging"
	write_checksums "$staging"
	rm -rf -- "$out"
	mv -- "$staging" "$out"
	printf 'build-dist.sh: wrote %s\n' "$out"
}

main "$@"
