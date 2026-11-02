#!/bin/bash
# Shared functions for Phasmid's build scripts. Adapted from
# ungoogled-chromium-portablelinux/scripts/shared.sh (BSD-3-Clause).
set -euo pipefail

phasmid_root() {
    local base
    base="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
    cd "${base}/.." >/dev/null 2>&1 && pwd
}

setup_paths() {
    _root="$(phasmid_root)"
    _build_dir="${PHASMID_BUILD_DIR:-${_root}/build}"
    _dl_cache="${_build_dir}/download_cache"
    _src_dir="${_build_dir}/src"
    _out_dir="${_src_dir}/out/${PHASMID_OUT:-Release}"
    _arch="${ARCH:-x64}"
    mkdir -p "${_dl_cache}"
}

step() { echo; echo "=== [$(date +%T)] $*"; }

fetch_sources() {
    local stamp="${_src_dir}/.downloaded.stamp"
    if [ -f "${stamp}" ]; then
        echo "sources already present in ${_src_dir}"
        return 0
    fi
    "${_root}/utils/downloads.py" retrieve -i "${_root}/downloads.ini" -c "${_dl_cache}"
    "${_root}/utils/downloads.py" unpack -i "${_root}/downloads.ini" -c "${_dl_cache}" "${_src_dir}"
    touch "${stamp}"
}

# Write a quilt directory for the upstream series with the entries in
# patches/phasmid/disabled-upstream.txt removed. Upstream patch files are
# never copied or edited; the directory holds symlinks and a filtered series.
write_filtered_upstream() {
    local dir="${_build_dir}/upstream-patches"
    rm -rf "${dir}"
    mkdir -p "${dir}"
    for d in core extra upstream-fixes; do
        [ -d "${_root}/patches/${d}" ] && ln -s "${_root}/patches/${d}" "${dir}/${d}"
    done
    local disabled
    disabled="$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e 's/[[:space:]]*$//' "${_root}/patches/phasmid/disabled-upstream.txt")"
    : > "${dir}/series"
    while IFS= read -r line; do
        local entry
        entry="$(printf '%s' "${line}" | sed -e 's/#.*//' -e 's/[[:space:]]*$//')"
        [ -z "${entry}" ] && continue
        if printf '%s\n' "${disabled}" | grep -qx -- "${entry}"; then
            echo "skipping upstream patch: ${entry}"
            continue
        fi
        echo "${entry}" >> "${dir}/series"
    done < "${_root}/patches/series"
    echo "${dir}"
}

apply_patches() {
    local stamp="${_src_dir}/.patched.stamp" head
    head="$(git -C "${_root}" rev-parse HEAD)"
    if [ -f "${stamp}" ]; then
        if [ "$(cat "${stamp}")" = "${head}" ]; then
            echo "patches already applied"
            return 0
        fi
        echo "error: source tree was patched from commit $(cat "${stamp}") but the repository is now at ${head}; remove ${_src_dir} (or use a fresh build dir) before building" >&2
        return 1
    fi
    "${_root}/utils/prune_binaries.py" "${_src_dir}" "${_root}/pruning.list"
    local upstream
    upstream="$(write_filtered_upstream | tail -1)"
    "${_root}/utils/patches.py" apply "${_src_dir}" "${upstream}" "${_root}/patches/phasmid"
    echo "${head}" > "${stamp}"
}

apply_domsub() {
    local stamp="${_src_dir}/.domsub.stamp" head
    head="$(git -C "${_root}" rev-parse HEAD)"
    if [ -f "${stamp}" ]; then
        if [ "$(cat "${stamp}")" = "${head}" ]; then
            echo "domain substitution already applied"
            return 0
        fi
        echo "error: source tree was patched from commit $(cat "${stamp}") but the repository is now at ${head}; remove ${_src_dir} (or use a fresh build dir) before building" >&2
        return 1
    fi
    rm -f "${_build_dir}/domsubcache.tar.gz"
    "${_root}/utils/domain_substitution.py" apply -r "${_root}/domain_regex.list" \
        -f "${_root}/domain_substitution.list" -c "${_build_dir}/domsubcache.tar.gz" "${_src_dir}"
    echo "${head}" > "${stamp}"
}

# args.gn = upstream flags.gn + gn/base.gn + gn/<flavor>.gn [+ gn/local.gn]
write_gn_args() {
    local flavor="${1:-release}"
    mkdir -p "${_out_dir}"
    {
        cat "${_root}/flags.gn" "${_root}/gn/base.gn" "${_root}/gn/${flavor}.gn"
        [ -f "${_root}/gn/local.gn" ] && cat "${_root}/gn/local.gn"
        echo "target_cpu = \"${_arch}\""
        echo "v8_target_cpu = \"${_arch}\""
        if command -v sccache >/dev/null 2>&1 && ! grep -q cc_wrapper "${_root}/gn/${flavor}.gn" "${_root}/gn/local.gn" 2>/dev/null; then
            echo 'cc_wrapper = "sccache"'
        fi
    } > "${_out_dir}/args.gn"
    echo "wrote ${_out_dir}/args.gn:"
    cat "${_out_dir}/args.gn"
}

# Domain substitution rewrites the download hosts the toolchain scripts use.
fix_tool_downloading() {
    sed -i 's/commondatastorage.9oo91eapis.qjz9zk/commondatastorage.googleapis.com/g' \
        "${_src_dir}/build/linux/sysroot_scripts/sysroots.json" \
        "${_src_dir}/tools/clang/scripts/update.py" \
        "${_src_dir}/tools/clang/scripts/build.py"
    sed -i 's/chromium.9oo91esource.qjz9zk/chromium.googlesource.com/g' \
        "${_src_dir}/tools/clang/scripts/build.py" \
        "${_src_dir}/tools/rust/build_rust.py" \
        "${_src_dir}/tools/rust/build_bindgen.py"
    sed -i 's/chrome-infra-packages.8pp2p8t.qjz9zk/chrome-infra-packages.appspot.com/g' \
        "${_src_dir}/tools/rust/build_rust.py"
}

setup_toolchain() {
    "${_src_dir}/tools/rust/update_rust.py"
    "${_src_dir}/tools/clang/scripts/update.py"
    if grep -q -F "use_sysroot = true" "${_out_dir}/args.gn" || grep -q -F "use_sysroot=true" "${_out_dir}/args.gn"; then
        "${_src_dir}/build/linux/sysroot_scripts/install-sysroot.py" --arch="${_arch}"
    fi
    mkdir -p "${_src_dir}/third_party/node/linux/node-linux-x64/bin"
    ln -sf "$(command -v node)" "${_src_dir}/third_party/node/linux/node-linux-x64/bin/node"
    mkdir -p "${_src_dir}/third_party/gperf/cipd/bin/"
    ln -sf "$(command -v gperf)" "${_src_dir}/third_party/gperf/cipd/bin/gperf"
    mkdir -p "${_src_dir}/third_party/dawn/tools/golang/linux-amd64/bin"
    ln -sf "$(command -v go)" "${_src_dir}/third_party/dawn/tools/golang/linux-amd64/bin/go"
    mkdir -p "${_src_dir}/buildtools/linux64-format"
    ln -sf "$(command -v clang-format)" "${_src_dir}/buildtools/linux64-format/clang-format"
    # Re-runnable: the esbuild path may already be our symlink; never rm -rf
    # through it, or the global install gets wiped.
    local esbuild="${_src_dir}/third_party/devtools-frontend/src/node_modules/esbuild"
    [ -x /usr/lib/node_modules/esbuild/bin/esbuild ] || npm -g install esbuild
    if [ -L "${esbuild}" ]; then rm -f "${esbuild}"; else rm -rf "${esbuild}"; fi
    ln -s /usr/lib/node_modules/esbuild/ "${esbuild}"

    local clang_bin="${_src_dir}/third_party/llvm-build/Release+Asserts/bin"
    export CC="${clang_bin}/clang" CXX="${clang_bin}/clang++" AR="${clang_bin}/llvm-ar" NM="${clang_bin}/llvm-nm" LLVM_BIN="${clang_bin}"
    local resource_dir
    resource_dir="$(${CC} --print-resource-dir)"
    export CXXFLAGS="${CXXFLAGS:-} -resource-dir=${resource_dir} -B${LLVM_BIN}"
    export CPPFLAGS="${CPPFLAGS:-} -resource-dir=${resource_dir} -B${LLVM_BIN}"
    export CFLAGS="${CFLAGS:-} -resource-dir=${resource_dir} -B${LLVM_BIN}"
}

gn_gen() {
    cd "${_src_dir}"
    ./tools/gn/bootstrap/bootstrap.py -o "${_out_dir}/gn" --skip-generate-buildfiles
    "${_out_dir}/gn" gen "${_out_dir}" --fail-on-unused-args
}
