# Phasmid tests

Everything that checks a built browser, plus the static checks that need none. Managed with [uv](https://docs.astral.sh/uv/); run every command from this directory.

```bash
uv sync                  # once
uv run pytest -q         # unit tests and static checks, no browser
```

| Directory      | What it does                                                          | Needs             |
| -------------- | --------------------------------------------------------------------- | ----------------- |
| `static/`      | patch headers and series, disabled-upstream reasons, profiles, schema | nothing           |
| `consistency/` | dumps every surface from every context, compares dumps and profiles   | a binary          |
| `guards/`      | one script per Phasmid patch, asserting its observable effect         | a Phasmid binary  |
| `masking/`     | public detection sites' verdicts, per mode                            | a binary, network |
| `inspector/`   | which Web APIs a page calls, with arguments and callers               | a binary built with `devtools/fingerprint-trace.patch` |

## Consistency harness

Launches a Chromium binary as a user would (its own process, a fixed `--remote-debugging-port`, no Playwright launch flags), loads one page from two loopback origins and dumps every surface from every context:

| Context          | How it is reached                                        |
| ---------------- | -------------------------------------------------------- |
| `main`           | the document on origin A                                 |
| `iframe_same`    | `<iframe>` on origin A                                   |
| `iframe_cross`   | `<iframe>` on origin B (a second port)                   |
| `worker`         | `new Worker()`                                           |
| `shared_worker`  | `new SharedWorker()`                                     |
| `service_worker` | registered at `/sw.js`, answered over a `MessageChannel` |
| headers          | every request the servers saw, keyed by what made it     |

The probe (`web/probe.js`) runs in all of them. Each section records `{error}` on failure, so a missing API is data, not an abort. OffscreenCanvas, WebGL and WebGPU also run inside the workers.

```bash
# Dump a binary; prints the cross-context, header-vs-JS and within-context reports.
uv run python -m consistency dump --binary /usr/bin/google-chrome-stable -o out/stock.json
uv run python -m consistency dump --binary <chrome> --profile ../profiles/examples/<id>.json \
    --baseline out/stock.json -o out/phasmid.json
uv run python -m consistency dump --binary <chrome> --headful -o out/headful.json

uv run python -m consistency diff out/stock.json out/phasmid.json
uv run python -m consistency contexts out/phasmid.json --baseline out/stock.json
uv run python -m consistency validate ../profiles/examples/*.json
uv run python -m consistency compare out/phasmid.json ../profiles/examples/<id>.json
```

Reports:

- **cross-context disagreements**: a key whose value differs between contexts. Stock Chrome has its own splits (worker-only processes resolve generic fonts differently, `decodingInfo.powerEfficient` flips off the main thread, cross-origin frames are permissions-policy restricted). With `--baseline`, only splits stock Chrome does not show are reported; Phasmid must add none.
- **header/JS mismatches**: a sent header that differs from its JavaScript counterpart (`User-Agent`, `Accept-Language`, `Sec-CH-UA*`, `Device-Memory`, `DPR`). A header that was not sent is listed as "not sent"; whether it should have been is answered by `diff` against stock.
- **within-context failures**: invariants stock Chrome holds inside one context: deterministic canvas and audio readback, sub-rectangle reads equal to the full read, OffscreenCanvas equal to the DOM canvas, WebGL1 and WebGL2 naming the same GPU, `Range` and `Element` rects agreeing, the UA version equal to `fullVersionList` and `uaFullVersion`.

`diff` ignores volatile keys (timing, quota, mDNS candidates). `compare` checks every schema field of one profile against the dump key it must appear at, in every context where that surface exists, plus the request headers and build invariants (the brand list is GREASE, `Chromium`, `Google Chrome`).

The baseline is a stock Google Chrome of the same major as `chromium_version.txt`. CI dumps it on the runner; nothing is checked in.

### Captures

A profile's device data comes from a real machine: a headful stock Chrome dump there, then `capture`.

```bash
uv run python -m consistency capture out/headful.json --id <id> --os-version 11 \
    --physical-memory-mb 32768 --disk-total-bytes 1000204886016 -o ../profiles/examples/<id>.json
```

To capture a machine without the harness, run the collector and reach it through a tunnel. It serves `collect.html` on two loopback ports (page and cross-origin iframe); a visit to the printed `/?k=<token>` link runs every context and saves the dump with its request headers to `out/captures/`. Both ports must be reached over https, or the page is not a secure context and loses `userAgentData`, `deviceMemory` and WebGPU.

```bash
# Tailscale
tailscale serve --bg --https=443 http://127.0.0.1:8800
tailscale serve --bg --https=8443 http://127.0.0.1:8801
uv run python -m consistency collect --cross https://<machine>.<tailnet>.ts.net:8443

# Cloudflare quick tunnels; pass the second one's URL
cloudflared tunnel --url http://127.0.0.1:8800
cloudflared tunnel --url http://127.0.0.1:8801
uv run python -m consistency collect --cross https://<second>.trycloudflare.com
```

Each token saves once; dumps are capped at 16 MB. `capture` accepts only Google Chrome dumps from a top-level page. What a page cannot read it derives and names in `captured.note`: GPU IDs from a Windows renderer string, RAM from `deviceMemory` and `jsHeapSizeLimit`, the disk from the quota. Linux GPU IDs come from the `chrome://gpu` report or `--gpu`, else are left out. Values given as flags win. The dump is kept under `profiles/data/captures/`, and `static/` fails for an example profile without one.

### Notes

- The binary is attached with `connect_over_cdp`, not launched by Playwright, whose launcher adds `--enable-automation`, `--remote-debugging-pipe` and feature overrides that change measured surfaces. A fixed non-zero debugging port does not set `AutomationControlled`; port 0, the pipe and `--enable-automation` do.
- Readiness is polled on `/json/version`; `DevToolsActivePort` is unreliable under `--headless=new`.
- `127.0.0.1` is potentially trustworthy, so secure-context APIs and Client Hints work without TLS. The first navigation stores the `Accept-CH` grant; the second carries the high-entropy hints.
- Headful runs need a display; CI uses `xvfb-run`.

## Guards

```bash
uv run python -m guards --binary <chrome> --profile <profile.json> [--patch <area>/<surface>] [--headful]
```

Runs `guards/<area>/<surface>.py` in parallel. Exit 1 is a failure, 3 a skip, anything else an error; an empty selection is refused. Each guard first proves its precondition took effect and fails as vacuous if it cannot. A guard skips only when the host cannot run it, e.g. the WebGL and WebGPU guards need the profile's GPU class.

## Masking report

Visits public detection pages (SannySoft, CreepJS, Brotector, the Fingerprint.com playground, BrowserScan, Pixelscan, Iphey) and records each page's own verdict. Informational: verdicts depend on the sites and the network, and the suite is not part of `All tests passed`. Run stock Chrome through the same modes; only a check stock passes and Phasmid fails is a regression.

```bash
uv run python -m masking run --binary /usr/bin/google-chrome-stable --mode headless --label stock
uv run python -m masking run --binary <chrome> --profile <profile.json> --mode headful --label phasmid [--site sannysoft]
../scripts/masking-docker.sh --binary <chrome> --profile <profile.json> --label phasmid   # xvfb mode in a container
uv run python -m masking report out/masking --markdown out/masking/report.md
```

Each site gets its own browser process and user data directory and writes `<out>/<label>/<mode>/<site>.json` plus a screenshot. `error` (the page did not render what the parser expects) is kept apart from `fail` and retried once. Modes: `headful` uses `$DISPLAY`, `headless` passes `--headless=new`, `xvfb` starts a private Xvfb. The report lists sites whose verdict differs between modes of one label, and sites a label fails where the stock baseline passes. `masking remote --provider kameleo|cloakbrowser` runs the same sites in a vendor's container for comparison.

## Fingerprint inspector

```bash
uv run python -m inspector run --binary <chrome> [--profile <p.json>] --url https://example.org --wait 30 -o out/inspect/example
uv run python -m inspector report out/inspect/example.json
uv run python -m inspector diff out/inspect/stock.json out/inspect/phasmid.json
```

The collector attaches to the browser target only (no `Runtime.enable`), starts tracing with `disabled-by-default-phasmid.fingerprint` before the first page opens, and opens each URL with `Target.createTarget`. `run` writes `<out>.json` and `<out>.md` and exits 1 on data loss or an empty trace. See [`docs/inspector.md`](../docs/inspector.md).

## Results

Every suite writes `out/results/<suite>.json` with per-test outcomes and the run id. `summarize.py --require <suites>` merges them into one table and one exit code; a required suite with no record, or one from another run, fails.
