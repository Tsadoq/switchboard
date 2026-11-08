#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="/opt/agent-board"
STATE_DIR="/var/lib/agent-board"
ENV_FILE="/etc/agent-board.env"
UNIT="/etc/systemd/system/agent-board.service"
ADMIN="/usr/local/sbin/board-admin"
UPDATER="/usr/local/sbin/agent-board-update"
UPDATE_DIR="/var/lib/agent-board-update"
UPDATE_CONF="/etc/agent-board-update.conf"
UPDATE_UNITS=(agent-board-update.service agent-board-update.path agent-board-update.timer)
DEFAULT_REMOTE="https://github.com/Tsadoq/switchboard.git"
DEFAULT_BRANCH="main"
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
agent-board.service unit, the $ADMIN wrapper for board-serve commands, and the updater
behind the board's Updates page ($UPDATER and its units). Run it as root from a clone of the
repository; it installs the clone's committed HEAD. $ENV_FILE and $UPDATE_CONF are created
only when they do not exist. Running it again upgrades in place.

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
	record_installed
}

record_installed() {
	git -C "$REPO" log -1 --format='%H%x00%s%x00%cI' HEAD | python3.13 -I -c '
import json, sys, time
commit, subject, date = sys.stdin.read().rstrip("\n").split("\0")
json.dump({"commit": commit, "subject": subject, "date": date, "installed_at": time.time()}, sys.stdout)
' >"$PREFIX/installed.json.new"
	chmod 0644 "$PREFIX/installed.json.new"
	mv -- "$PREFIX/installed.json.new" "$PREFIX/installed.json"
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
	install_updater
	if [[ ! -e $ENV_FILE ]]; then
		say "writing $ENV_FILE from the example; edit it before agents connect"
		install -m 0600 -o root -g root "$REPO/deploy/agent-board.env.example" "$ENV_FILE"
	fi
	systemctl daemon-reload
}

install_updater() {
	local unit
	install -m 0755 "$REPO/deploy/agent-board-update" "$UPDATER"
	for unit in "${UPDATE_UNITS[@]}"; do
		install -m 0644 "$REPO/deploy/$unit" "/etc/systemd/system/$unit"
	done
	install -d -m 0755 -o root -g root "$UPDATE_DIR"
	[[ -e $UPDATE_CONF ]] && return 0
	local remote branch
	remote="$(git -C "$REPO" remote get-url origin 2>/dev/null || echo "$DEFAULT_REMOTE")"
	branch="$(git -C "$REPO" symbolic-ref --short -q HEAD || echo "$DEFAULT_BRANCH")"
	say "writing $UPDATE_CONF: the Updates page follows $branch at $remote"
	sed -e "s|^REMOTE=.*|REMOTE=${remote//[\\|&]/\\&}|" -e "s|^BRANCH=.*|BRANCH=$branch|" \
		"$REPO/deploy/agent-board-update.conf.example" >"$UPDATE_CONF"
	chmod 0644 "$UPDATE_CONF"
}

enable_updater() {
	if ((!start)); then
		systemctl enable -q agent-board-update.path agent-board-update.timer
		return 0
	fi
	systemctl enable -q --now agent-board-update.path agent-board-update.timer
	if [[ -z ${AGENT_BOARD_UPDATING:-} ]]; then
		systemctl start --no-block agent-board-update.service
	fi
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
	if ! grep -q '^REMOTE=https://' "$UPDATE_CONF"; then
		say "$UPDATE_CONF does not fetch over https://; make sure root can fetch it without a password or key"
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
	enable_updater
	remind
}

main "$@"
