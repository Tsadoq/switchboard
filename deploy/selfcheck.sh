#!/usr/bin/env bash
set -euo pipefail

BOARD_URL_DEFAULT="http://127.0.0.1:8080"
CHECKSUMS="SHA256SUMS"
CURL_MAX_TIME=30
USAGE_EXIT=2

board=""
work=""

usage() {
	cat <<EOF
Usage: selfcheck.sh [BOARD_URL]

Checks a deployed board through the URL agents use (default: $BOARD_URL_DEFAULT):
/healthz answers ok, /install.sh names BOARD_URL as its default board (so BOARD_PUBLIC_URL
is right), and every file /dist/$CHECKSUMS lists downloads with the digest it lists.
Exits 0 when every check passes and 1 at the first that fails.
EOF
}

die() {
	printf 'selfcheck.sh: %s\n' "$*" >&2
	exit 1
}

usage_error() {
	printf 'selfcheck.sh: %s\n' "$*" >&2
	usage >&2
	exit "$USAGE_EXIT"
}

parse_args() {
	if [[ ${1:-} == -h || ${1:-} == --help ]]; then
		usage
		exit 0
	fi
	(($# <= 1)) || usage_error "expected at most one BOARD_URL argument"
	board="${1:-$BOARD_URL_DEFAULT}"
	board="${board%/}"
	[[ $board =~ ^https?://[^/]+$ ]] || usage_error "BOARD_URL must be http(s)://host with no path, got $board"
}

fetch() {
	curl -fsS --proto '=http,https' --max-time "$CURL_MAX_TIME" -o "$2" "$board$1" || die "cannot fetch $board$1"
}

check_health() {
	fetch /healthz "$work/healthz"
	grep -qE '"status"[[:space:]]*:[[:space:]]*"ok"' "$work/healthz" || die "/healthz did not answer ok: $(<"$work/healthz")"
	printf 'ok  /healthz: %s\n' "$(<"$work/healthz")"
}

check_installer() {
	fetch /install.sh "$work/install.sh"
	if ! grep -qxF "BOARD_URL_DEFAULT=\"$board\"" "$work/install.sh"; then
		die "/install.sh does not default to $board: check BOARD_PUBLIC_URL in /etc/agent-board.env"
	fi
	printf 'ok  /install.sh defaults to %s\n' "$board"
}

check_dist() {
	local digest name count=0
	fetch "/dist/$CHECKSUMS" "$work/$CHECKSUMS"
	mkdir -- "$work/dist"
	while read -r digest name; do
		name="${name#\*}"
		[[ $digest =~ ^[0-9a-f]{64}$ && -n $name ]] || die "malformed $CHECKSUMS line: $digest $name"
		if [[ $name == /* || "/$name/" == */../* ]]; then die "refusing unsafe path in $CHECKSUMS: $name"; fi
		mkdir -p -- "$(dirname -- "$work/dist/$name")"
		fetch "/dist/$name" "$work/dist/$name"
		count=$((count + 1))
	done <"$work/$CHECKSUMS"
	((count > 0)) || die "/dist/$CHECKSUMS lists no files"
	(cd -- "$work/dist" && sha256sum --check --quiet --strict -- "../$CHECKSUMS") ||
		die "a file under /dist does not match /dist/$CHECKSUMS"
	printf 'ok  /dist: all %d files match %s\n' "$count" "$CHECKSUMS"
}

main() {
	parse_args "$@"
	work="$(mktemp -d)"
	trap 'rm -rf -- "$work"' EXIT
	check_health
	check_installer
	check_dist
}

main "$@"
