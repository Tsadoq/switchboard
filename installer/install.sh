#!/usr/bin/env bash
set -euo pipefail

BOARD_URL_DEFAULT="@BOARD_PUBLIC_URL@"
SKILL_NAME="agent-board"
DIST_SKILL_DIR="skill/$SKILL_NAME"
CHECKSUMS="SHA256SUMS"
WHEEL_PATTERN='^agent_board-[^/]+\.whl$'
UNIT_TEMPLATE="agent-board-runner@.service"
UNIT_DIR="/etc/systemd/system"
PYTHON_VERSION="3.13"
SKILL_DIR="$HOME/.agents/skills/$SKILL_NAME"
CLAUDE_SKILL_LINK="$HOME/.claude/skills/$SKILL_NAME"
BOARD_HOME="$HOME/.agents/board"
INSTALLED_WHEEL_MARKER="$BOARD_HOME/installed-wheel.sha256"
USAGE_EXIT=2

board=""
dist_url=""
with_runner=0
with_skill=1
dry_run=0
work=""
fetched=""
wheel=""
bin_dir=""
skill_files=()

usage() {
	cat <<EOF
Usage: install.sh [--board URL] [--dist-url URL] [--with-runner] [--no-skill] [--dry-run]

Installs the agent-board CLI with uv, creates each kind's board state directory, and
publishes the agent-board skill to ~/.agents/skills (linked into ~/.claude/skills).
Run it as the agent user. A second run changes nothing unless the board ships a new
version.

  --board URL     the board (default: ${BOARD_URL_DEFAULT})
  --dist-url URL  where to download from (default: <board>/dist)
  --with-runner   also install and enable agent-board-runner@\$USER (uses sudo)
  --no-skill      leave the skill directories alone
  --dry-run       download and verify, then print what would change
  --help          show this help
EOF
}

die() {
	printf 'install.sh: %s\n' "$*" >&2
	exit 1
}

usage_error() {
	printf 'install.sh: %s\n' "$*" >&2
	usage >&2
	exit "$USAGE_EXIT"
}

change() {
	if ((dry_run)); then
		printf 'would run:'
		printf ' %q' "$@"
		printf '\n'
	else
		"$@"
	fi
}

parse_args() {
	board="$BOARD_URL_DEFAULT"
	while (($#)); do
		case "$1" in
		--board | --dist-url)
			(($# >= 2)) || usage_error "$1 needs a URL"
			if [[ $1 == --board ]]; then board="$2"; else dist_url="$2"; fi
			shift 2
			;;
		--with-runner) with_runner=1; shift ;;
		--no-skill) with_skill=0; shift ;;
		--dry-run) dry_run=1; shift ;;
		-h | --help) usage; exit 0 ;;
		*) usage_error "unknown option: $1" ;;
		esac
	done
	board="${board%/}"
	[[ $board =~ ^https?:// ]] || usage_error "no board URL: pass --board URL"
	dist_url="${dist_url:-$board/dist}"
	dist_url="${dist_url%/}"
}

check_prerequisites() {
	if ((EUID == 0)); then
		die "run install.sh as the agent user, not root; --with-runner calls sudo itself"
	fi
	local tools=(uv curl sha256sum cmp agent-sign) missing=() tool
	if ((with_runner)); then tools+=(sudo systemctl); fi
	for tool in "${tools[@]}"; do
		command -v "$tool" >/dev/null || missing+=("$tool")
	done
	if ((${#missing[@]})); then die "missing required commands: ${missing[*]}"; fi
	bin_dir="$(uv tool dir --bin)"
	if ((with_skill)); then check_claude_skill_link; fi
	if ((with_runner)); then check_runner_paths; fi
}

claude_skill_link_is_ours() {
	[[ -L $CLAUDE_SKILL_LINK && "$(readlink -m -- "$CLAUDE_SKILL_LINK")" == "$(readlink -m -- "$SKILL_DIR")" ]]
}

check_claude_skill_link() {
	if [[ ! -e $CLAUDE_SKILL_LINK && ! -L $CLAUDE_SKILL_LINK ]] || claude_skill_link_is_ours; then
		return 0
	fi
	die "$CLAUDE_SKILL_LINK exists and is not a symlink to $SKILL_DIR." \
		"The rule: each shared skill lives once in ~/.agents/skills/<name>/" \
		"and ~/.claude/skills/<name> is only a symlink to it. Move $CLAUDE_SKILL_LINK aside" \
		"and run the installer again."
}

check_runner_paths() {
	local expected
	expected="/home/$(id -un)/.local/bin"
	if [[ $bin_dir != "$expected" ]]; then
		die "$UNIT_TEMPLATE runs $expected/agent-board, but uv installs tools into $bin_dir"
	fi
}

fetch() {
	curl -fsSL --proto '=http,https' -o "$2" "$dist_url/$1" || die "cannot download $dist_url/$1"
}

listed_names() {
	awk '{ sub(/^\*/, "", $2); print $2 }' "$work/$CHECKSUMS"
}

listed_names_under() {
	listed_names | awk -v prefix="$1/" 'index($0, prefix) == 1'
}

listed_digest() {
	awk -v name="$1" '{ sub(/^\*/, "", $2) } $2 == name { print $1; exit }' "$work/$CHECKSUMS"
}

wheel_name() {
	local names
	names="$(listed_names | grep -E "$WHEEL_PATTERN" || true)"
	if [[ -z $names || $names == *$'\n'* ]]; then
		die "$CHECKSUMS at $dist_url must list exactly one agent_board wheel"
	fi
	printf '%s\n' "$names"
}

fetch_verified() {
	local name="$1" expected actual
	if [[ $name == /* || "/$name/" == */../* ]]; then die "refusing unsafe path in $CHECKSUMS: $name"; fi
	expected="$(listed_digest "$name")"
	[[ $expected =~ ^[0-9a-f]{64}$ ]] || die "$CHECKSUMS has no sha256 for $name"
	mkdir -p "$(dirname "$fetched/$name")"
	fetch "$name" "$fetched/$name"
	actual="$(sha256sum "$fetched/$name" | cut -d' ' -f1)"
	if [[ $actual != "$expected" ]]; then
		die "checksum mismatch for $name: $CHECKSUMS says $expected, the download is $actual"
	fi
}

download() {
	work="$(mktemp -d)"
	trap 'rm -rf -- "$work"' EXIT
	fetched="$work/files"
	fetch "$CHECKSUMS" "$work/$CHECKSUMS"
	wheel="$(wheel_name)"
	local wanted=("$wheel") name
	if ((with_skill)); then
		mapfile -t skill_files < <(listed_names_under "$DIST_SKILL_DIR")
		if ((${#skill_files[@]} == 0)); then die "$CHECKSUMS lists no files under $DIST_SKILL_DIR/"; fi
		wanted+=("${skill_files[@]}")
	fi
	if ((with_runner)); then wanted+=("$UNIT_TEMPLATE"); fi
	for name in "${wanted[@]}"; do fetch_verified "$name"; done
}

record_installed_wheel() {
	mkdir -p "$BOARD_HOME"
	printf '%s\n' "$1" >"$INSTALLED_WHEEL_MARKER"
}

# `uv tool install --reinstall` always rewrites the executables, so the recorded wheel digest
# is what keeps a second run from touching anything.
install_cli() {
	local digest
	digest="$(listed_digest "$wheel")"
	if [[ -x $bin_dir/agent-board && -f $INSTALLED_WHEEL_MARKER && "$(<"$INSTALLED_WHEEL_MARKER")" == "$digest" ]]; then
		printf 'agent-board: %s is already installed in %s\n' "$wheel" "$bin_dir"
		return 0
	fi
	change uv tool install --reinstall --python "$PYTHON_VERSION" "$fetched/$wheel"
	change record_installed_wheel "$digest"
}

init_board_state() {
	change "$bin_dir/agent-board" init --all-kinds --board "$board"
}

prune_unshipped_skill_files() {
	local installed relative name shipped
	[[ -d $SKILL_DIR ]] || return 0
	while IFS= read -r -d '' installed; do
		relative="${installed#"$SKILL_DIR"/}"
		shipped=0
		for name in "${skill_files[@]}"; do
			if [[ ${name#"$DIST_SKILL_DIR"/} == "$relative" ]]; then
				shipped=1
				break
			fi
		done
		if ((!shipped)); then change rm -f "$installed"; fi
	done < <(find "$SKILL_DIR" -type f -print0)
}

install_skill() {
	local name downloaded installed
	for name in "${skill_files[@]}"; do
		downloaded="$fetched/$name"
		installed="$SKILL_DIR/${name#"$DIST_SKILL_DIR"/}"
		if cmp -s "$downloaded" "$installed"; then continue; fi
		if [[ ! -d ${installed%/*} ]]; then change mkdir -p "${installed%/*}"; fi
		change install -m 0644 "$downloaded" "$installed"
	done
	prune_unshipped_skill_files
	if ! claude_skill_link_is_ours; then
		if [[ ! -d ${CLAUDE_SKILL_LINK%/*} ]]; then change mkdir -p "${CLAUDE_SKILL_LINK%/*}"; fi
		change ln -s "$SKILL_DIR" "$CLAUDE_SKILL_LINK"
	fi
}

runner_started_before_any() {
	local unit="$1" started file
	shift
	started="$(systemctl show "$unit" --property=ActiveEnterTimestamp --value)"
	[[ -n $started ]] || return 1
	started="$(date -d "$started" +%s)"
	for file in "$@"; do
		if [[ -e $file ]] && (($(stat -c %Y "$file") > started)); then return 0; fi
	done
	return 1
}

install_runner() {
	local unit installed="$UNIT_DIR/$UNIT_TEMPLATE"
	unit="agent-board-runner@$(id -un).service"
	if ! cmp -s "$fetched/$UNIT_TEMPLATE" "$installed"; then
		change sudo install -m 0644 "$fetched/$UNIT_TEMPLATE" "$installed"
		change sudo systemctl daemon-reload
	fi
	if ! systemctl is-enabled --quiet "$unit"; then change sudo systemctl enable "$unit"; fi
	if ! systemctl is-active --quiet "$unit"; then
		change sudo systemctl start "$unit"
	elif runner_started_before_any "$unit" "$INSTALLED_WHEEL_MARKER" "$installed"; then
		change sudo systemctl restart "$unit"
	fi
}

print_next_steps() {
	cat <<EOF

Next steps:
- Once per kind, open one session so the harness trusts its working directory:
    agent-board wake-test <kind>
- Add the lines in $dist_url/AGENTS.snippet.md
  to ~/.agents/AGENTS.md.
EOF
	if [[ ":$PATH:" != *":$bin_dir:"* ]]; then
		printf -- '- Put %s on PATH (uv tool update-shell does this).\n' "$bin_dir"
	fi
	if ((!with_runner)); then
		printf -- '- To have board wakes start sessions on this box, run again with --with-runner.\n'
	fi
	cat <<EOF
- The runner's settings are documented in $dist_url/runner.toml.example;
  copy it to /etc/agent-board-runner.toml or $BOARD_HOME/runner.toml to change them.
EOF
}

main() {
	parse_args "$@"
	check_prerequisites
	download
	install_cli
	init_board_state
	if ((with_skill)); then install_skill; fi
	if ((with_runner)); then install_runner; fi
	print_next_steps
}

main "$@"
