#!/bin/bash
# Prune binaries, apply the ungoogled-chromium series minus disabled-upstream.txt,
# then the Phasmid series, then domain substitution.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_paths
step "patches"
apply_patches
step "domain substitution"
apply_domsub
