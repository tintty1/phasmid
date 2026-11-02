#!/bin/bash
# Run the masking report in the Playwright image, for modes the host cannot
# provide (xvfb: the image ships Xvfb and every Chromium runtime library).
#   ./scripts/masking-docker.sh --binary <host path to chrome> [--profile <profile.json>] \
#       [--mode xvfb] [--label phasmid] [any other `python -m masking run` argument]
# The repository is mounted read-only at /repo, the binary's directory at
# /browser, and tests/out read-write so records and screenshots land on the
# host. The container runs as the host user and keeps its venv and uv cache
# inside the container, so the host's tests/.venv is never touched.
# The image supplies Xvfb and system libraries only; Playwright itself comes
# from tests/uv.lock and the browser is the mounted binary. Its tag is the
# newest published one not newer than the locked Playwright.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${PHASMID_MASKING_IMAGE:-mcr.microsoft.com/playwright/python:v1.63.0-noble}"

binary=""
profile=""
mode="xvfb"
passthrough=()
while [ $# -gt 0 ]; do
	case "$1" in
	--binary) binary="$2"; shift 2 ;;
	--profile) profile="$2"; shift 2 ;;
	--mode) mode="$2"; shift 2 ;;
	*) passthrough+=("$1"); shift ;;
	esac
done
[ -n "${binary}" ] || { echo "usage: $0 --binary <chrome> [--profile <json>] [masking run args]" >&2; exit 2; }
[ -x "${binary}" ] || { echo "binary not executable: ${binary}" >&2; exit 2; }

binary="$(readlink -f "${binary}")"
mounts=(-v "${root}:/repo:ro" -v "$(dirname "${binary}"):/browser:ro")
mkdir -p "${root}/tests/out"
mounts+=(-v "${root}/tests/out:/repo/tests/out")
args=(--binary "/browser/$(basename "${binary}")" --mode "${mode}")

if [ -n "${profile}" ]; then
	profile="$(readlink -f "${profile}")"
	[ -f "${profile}" ] || { echo "profile not found: ${profile}" >&2; exit 2; }
	case "${profile}" in
	"${root}"/*) args+=(--profile "/repo/${profile#"${root}"/}") ;;
	*) mounts+=(-v "$(dirname "${profile}"):/profile:ro"); args+=(--profile "/profile/$(basename "${profile}")") ;;
	esac
	# A profile claiming Widevine needs Google Chrome's CDM, which the image
	# lacks. Lend the host's.
	cdm=/opt/google/chrome/WidevineCdm
	if [ -d "${cdm}" ]; then
		mounts+=(-v "${cdm}:/widevine:ro"); args+=(--arg=--phasmid-widevine-cdm=/widevine)
	fi
	# The families a Linux profile lists are the host's installed fonts.
	mounts+=(-v /usr/share/fonts:/usr/share/fonts:ro)
	[ -d "${HOME}/.local/share/fonts" ] && mounts+=(-v "${HOME}/.local/share/fonts:/usr/local/share/fonts/host-user:ro")
	# A Windows profile reads its fonts from the store under $XDG_DATA_HOME.
	store="${XDG_DATA_HOME:-${HOME}/.local/share}/phasmid/fonts"
	[ -d "${store}" ] && mounts+=(-v "${store}:/xdg/phasmid/fonts:ro" -e XDG_DATA_HOME=/xdg)
fi

# Default output is tests/out/masking, i.e. the read-write mount.
# Docker's default seccomp profile refuses the user namespace Chrome's
# sandbox needs. Lifting the filter keeps the browser sandboxed and stock;
# --no-sandbox instead would show an infobar that shrinks the viewport.
exec docker run --rm --init \
	--user "$(id -u):$(id -g)" \
	--security-opt seccomp=unconfined \
	--shm-size=2g \
	-e HOME=/tmp/home \
	-e UV_PROJECT_ENVIRONMENT=/tmp/venv \
	-e UV_CACHE_DIR=/tmp/uv-cache \
	-e PHASMID_RUN_ID="${PHASMID_RUN_ID:-}" \
	-e PYTHONDONTWRITEBYTECODE=1 \
	"${mounts[@]}" \
	"${image}" \
	bash -euo pipefail -c '
		mkdir -p "$HOME"
		pip install --quiet --user --disable-pip-version-check uv
		cd /repo/tests
		"$HOME/.local/bin/uv" sync --frozen --quiet
		exec "$HOME/.local/bin/uv" run --frozen python -m masking run "$@"
	' masking "${args[@]}" "${passthrough[@]}"
