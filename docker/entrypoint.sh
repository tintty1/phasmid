#!/bin/bash
# Start Phasmid with CDP on port 9222. Arguments are passed to the browser,
# e.g. --phasmid-profile=/profile.json.
#   PHASMID_HEADFUL=1          run headful under Xvfb instead of --headless
#   PHASMID_XVFB_SCREEN=WxHxD  Xvfb screen, at least the profile's screen size
# Chrome binds its debugging port to loopback only, so socat forwards it.
set -euo pipefail

internal_port=9221
args=(
    --remote-debugging-port="${internal_port}"
    --user-data-dir="${HOME}/data"
    --no-first-run
    --no-default-browser-check
)

socat "TCP-LISTEN:9222,fork,reuseaddr" "TCP:127.0.0.1:${internal_port}" &

if [ "${PHASMID_HEADFUL:-0}" = 1 ]; then
    exec xvfb-run --auto-servernum \
        --server-args="-screen 0 ${PHASMID_XVFB_SCREEN:-3840x2160x24}" \
        phasmid-chrome "${args[@]}" "$@"
fi
exec phasmid-chrome --headless "${args[@]}" "$@"
