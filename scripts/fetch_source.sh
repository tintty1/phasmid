#!/bin/bash
# Download and unpack the Chromium source tarball for chromium_version.txt into build/src.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_paths
step "fetch sources ($(cat "${_root}/chromium_version.txt"))"
fetch_sources
