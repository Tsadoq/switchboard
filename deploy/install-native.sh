#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="/opt/agent-board"
STATE_DIR="/var/lib/agent-board"
ENV_FILE="/etc/agent-board.env"
UNIT="/etc/systemd/system/agent-board.service"
ADMIN="/usr/local/sbin/board-admin"
UV_CACHE="/var/cache/agent-board-uv"
UV_VERSION="0.13.7"
BOARD_USER="board"
HEALTHZ="http://127.0.0.1:8080/healthz"
HEALTHZ_TRIES=30

start=1
work=""

usage() {
	cat <<USAGE
Usage: install-native.sh [--no-start]

Installs or upgrades the board on this host without a container: the code at $PREFIX/src,
a venv at $PREFIX/.venv, the /dist bundle at $PREFIX/dist, state in $STATE_DIR, the
agent-board.service unit and the $ADMIN wrapper for board-serve commands. Run it as root
from a clone of the repository; it installs the clone's committed HEAD. $ENV_FILE is created
from the example only when it does not exist. Running it again upgrades in place.

  --no-start  install everything but leave the service stopped
USAGE
}

die() {
	printf 'install-native.sh: %s\n' "$*" >&2
	exit 1
}

say() {
	printf '==> %s\n' "$*"
}

cleanup() {
	if [[ -n $work ]]; then
		rm -rf -- "$work"
	fi
}

parse_args() {
	while (($#)); do
		case "$1" in
		--no-start) start=0 ;;
		-h | --help)
			usage
			exit 0
			;;
		*) die "unknown argument: $1 (see --help)" ;;
		esac
		shift
	done
}

check_host() {
	[[ $(id -u) -eq 0 ]] || die "run as root"
	command -v systemctl >/dev/null || die "systemd is required"
	git -C "$REPO" rev-parse --verify -q HEAD >/dev/null || die "$REPO is not a git clone with a commit"
	if [[ -f $UNIT ]] && grep -q podman "$UNIT"; then
		die "$UNIT runs the Podman image; stop and remove it before switching to the native install"
	fi
}

install_packages() {
	local missing=()
	command -v python3.13 >/dev/null || missing+=(python3)
	command -v ssh-keygen >/dev/null || missing+=(openssh-client)
	command -v curl >/dev/null || missing+=(curl ca-certificates)
	((${#missing[@]})) || return 0
	command -v apt-get >/dev/null || die "install ${missing[*]} (Python 3.13, OpenSSH, curl) and run again"
	say "installing ${missing[*]}"
	apt-get update -q
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${missing[@]}"
	command -v python3.13 >/dev/null || die "this distribution has no python3.13; use Debian 13 or Ubuntu 25.04+, or the Podman install"
}

uv_target() {
	case "$(uname -m)" in
	x86_64) echo x86_64-unknown-linux-gnu ;;
	aarch64) echo aarch64-unknown-linux-gnu ;;
	*) die "no uv build for $(uname -m)" ;;
	esac
}

install_uv() {
	if command -v uv >/dev/null && [[ $(uv --version) == "uv $UV_VERSION"* ]]; then
		return 0
	fi
	local target url
	target="$(uv_target)"
	url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$target.tar.gz"
	say "installing uv $UV_VERSION"
	curl -fsSL -o "$work/uv.tar.gz" "$url"
	curl -fsSL -o "$work/uv.tar.gz.sha256" "$url.sha256"
	(cd -- "$work" && sed 's/ .*$/  uv.tar.gz/' uv.tar.gz.sha256 | sha256sum -c --quiet -)
	tar -xzf "$work/uv.tar.gz" -C "$work"
	install -m 0755 "$work/uv-$target/uv" "$work/uv-$target/uvx" /usr/local/bin/
}

create_user() {
	id -u "$BOARD_USER" >/dev/null 2>&1 && return 0
	say "creating the $BOARD_USER system user"
	useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home \
		--shell /usr/sbin/nologin "$BOARD_USER"
}

install_source() {
	say "installing $(git -C "$REPO" rev-parse --short HEAD) into $PREFIX/src"
	install -d -m 0755 "$PREFIX"
	rm -rf -- "$PREFIX/src.new"
	install -d -m 0755 "$PREFIX/src.new"
	git -C "$REPO" archive --format=tar HEAD | tar -x -C "$PREFIX/src.new"
	rm -rf -- "$PREFIX/src.old"
	if [[ -d $PREFIX/src ]]; then
		mv -- "$PREFIX/src" "$PREFIX/src.old"
	fi
	mv -- "$PREFIX/src.new" "$PREFIX/src"
	rm -rf -- "$PREFIX/src.old"
}

build() {
	say "building the venv and /dist"
	export UV_PROJECT_ENVIRONMENT="$PREFIX/.venv" UV_CACHE_DIR="$UV_CACHE" UV_PYTHON_DOWNLOADS=never \
		UV_NO_DEV=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
	(cd -- "$PREFIX/src" && uv sync --locked --no-editable --python python3.13 -q)
	"$PREFIX/src/scripts/build-dist.sh" "$PREFIX/dist"
	chmod -R a+rX "$PREFIX"
}

install_files() {
	install -m 0644 "$REPO/deploy/agent-board-native.service" "$UNIT"
	install -m 0755 "$REPO/deploy/board-admin" "$ADMIN"
	if [[ ! -e $ENV_FILE ]]; then
		say "writing $ENV_FILE from the example; edit it before agents connect"
		install -m 0600 -o root -g root "$REPO/deploy/agent-board.env.example" "$ENV_FILE"
	fi
	systemctl daemon-reload
}

start_service() {
	if ((!start)); then
		say "installed; start it with: systemctl enable --now agent-board"
		return 0
	fi
	systemctl enable -q agent-board.service
	systemctl restart agent-board.service
	local i
	for ((i = 0; i < HEALTHZ_TRIES; i++)); do
		if curl -fsS "$HEALTHZ" 2>/dev/null | grep -q '"status":"ok"'; then
			say "healthy: $(curl -fsS "$HEALTHZ")"
			return 0
		fi
		sleep 1
	done
	journalctl -u agent-board.service -n 40 --no-pager >&2 || true
	die "the board did not answer $HEALTHZ"
}

remind() {
	if ! grep -q '^BOARD_HUMAN_PASSWORD_HASH=.' "$ENV_FILE"; then
		say "no human login yet: run 'board-admin hash-password', put the line in $ENV_FILE, then 'systemctl restart agent-board'"
	fi
	if ! grep -q '^BOARD_PUBLIC_URL=https://' "$ENV_FILE" || grep -q '^BOARD_PUBLIC_URL=https://board.example.com$' "$ENV_FILE"; then
		say "set BOARD_PUBLIC_URL in $ENV_FILE to the board's real https:// URL"
	fi
}

main() {
	parse_args "$@"
	check_host
	work="$(mktemp -d)"
	trap cleanup EXIT
	install_packages
	install_uv
	create_user
	install_source
	build
	install_files
	start_service
	remind
}

main "$@"
