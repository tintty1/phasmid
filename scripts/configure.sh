#!/bin/bash
# Write args.gn for a flavor (ci|release|local), set up the toolchain, run gn gen.
#   ./scripts/configure.sh release
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
flavor="${1:-release}"
[ -f "$(phasmid_root)/gn/${flavor}.gn" ] || { echo "unknown flavor ${flavor}; see gn/"; exit 2; }
setup_paths
step "gn args (${flavor})"
write_gn_args "${flavor}"
step "toolchain"
fix_tool_downloading
setup_toolchain
step "gn gen"
gn_gen
echo
echo "next: autoninja -C ${_out_dir} chrome chromedriver"
