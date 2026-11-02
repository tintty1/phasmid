# Fingerprint inspector

The inspector records which Web APIs a page calls, in every context (main
frame, iframes, dedicated, shared and service workers), with the arguments,
the result and the script and line that made the call. It answers "what does
this detector actually read, and what did we tell it?" without anything the
page can observe. Use it to debug a masking-site verdict, to see which
surfaces a detector depends on, and to compare a Windows profile against a
Linux one or against stock Chrome.

## How it works

Two parts: a browser patch that emits trace events, and a collector in
`tests/inspector/` that records and summarizes them.

### The browser side

`patches/phasmid/devtools/fingerprint-trace.patch` changes Blink's bindings
generator. For every member listed in
`third_party/blink/renderer/bindings/phasmid_fingerprint_allowlist.txt`, the
generated V8 callback (getter, setter, operation or constructor) starts with a
`PhasmidFingerprintTraceScope`. When the trace category
`disabled-by-default-phasmid.fingerprint` is enabled, the scope emits one
event per call with:

- the API name as Blink names it: `Navigator.hardwareConcurrency.get`,
  `WebGLRenderingContext.getParameter`, `OfflineAudioContext.constructor`;
- the raw arguments;
- the return value, or `threw`;
- the caller: URL, line and column of up to three stack frames;
- the process, thread and world.

A returned promise gets an identity hash; a second event, emitted where every
Blink promise resolver settles (`ScriptPromiseResolverBase`), repeats the hash
with the state and the value, and the collector joins the two. Three more
hooks cover what is not a Web API member: `CSS.mediaFeature` (every media
feature evaluated, from stylesheets and `matchMedia`), `CSS.fontFamily` (every
family lookup with its outcome: webfont, platform or miss) and
`Intl.*.prototype.resolvedOptions` (`devtools/fingerprint-trace-intl.patch`,
in V8).

Values are described without running page code: primitives as they are,
strings as a 1 KiB UTF-8 prefix plus their length and an FNV-1a 64 hash of the
UTF-16 code units, Blink objects by interface name, typed arrays by their
contents, plain objects through their own data properties. The hook never
calls `toString`, never reads a getter on a page object, never touches a Proxy
and never attaches `then` to a promise.

With the category off, a listed call costs one atomic load. Nothing changes
for the page either way: the guard `tests/guards/devtools/fingerprint-trace.py`
checks that every context reports identical values with tracing on and off.
The committed allowlist ships in every build; there is no separate inspector
build and the GN argument `enable_blink_bindings_tracing` is never used.

### The collector

`python -m inspector run` launches the browser with a debugging port, attaches
to the browser target only (a standard-library WebSocket; no Playwright, no
`Runtime.enable`), starts CDP `Tracing` with the category before any page
exists, opens the URL with `Target.createTarget`, waits, and stops tracing.
Child processes started later (service worker, shared worker, cross-site
iframe renderers) inherit the tracing session, so every context is covered.
Detectors see a browser with no client attached to their page.

## Usage

From `tests/`, after `uv sync`:

```bash
# Trace one page. --profile is optional; without it the binary is plain Chromium.
uv run python -m inspector run \
  --binary <phasmid chrome> \
  --profile ../profiles/examples/win11-uhd600-1440x960.json \
  --url https://demo.fingerprint.com/playground \
  --wait 40 \
  -o out/inspector/fpcom-win

# Rebuild the Markdown summary from a saved trace.
uv run python -m inspector report out/inspector/fpcom-win.json

# Compare two traces.
uv run python -m inspector diff out/inspector/fpcom-linux.json out/inspector/fpcom-win.json > out/inspector/diff.md
```

Options of `run`:

| Option | Meaning |
| --- | --- |
| `--binary` | The browser to trace. It must contain `devtools/fingerprint-trace.patch`; any other binary gives an empty trace. |
| `--profile` | Phasmid profile to load. |
| `--url` | Page to open. Repeat for several pages. |
| `--wait` | Seconds to wait per page (default 20). Detectors that post their result late need 30 to 40. |
| `--until-title` | Stop waiting as soon as the page title equals this string. |
| `--headful` | Open a window instead of `--headless=new`. |
| `--arg` | An extra browser switch, repeatable. |
| `-o` | Output path without extension. |

`run` exits 1 when the browser reports trace data loss or the trace is empty.

### Output

`<out>.json` holds the raw events and a `parsed` object:

- `parsed.calls`: one entry per call, with `api`, `args`, `ret` (or `threw`),
  `caller` (list of `{url, line, column}`), `process`, `thread`, `world`,
  `main_world`, and for a returned promise `promise` (its identity hash) and
  `settled` (state and value).
- `parsed.css`, `parsed.fonts`, `parsed.intl`: the media-feature, font-lookup
  and Intl events.
- `parsed.data_loss`, `parsed.event_count`.

`<out>.md` lists the calls per thread with API, arguments, result and caller.

`diff` keys calls by thread kind, calling script path, API and arguments, so
two runs on different ports or with different tab ids compare. Each line is
one key whose values differ, or which only one trace has:

```
Navigator.platform.get() [CrRendererMain /web/v4/...]: ['"Linux x86_64"'] -> ['"Win32"']
```

### Narrowing to the detector's own calls

A page's own scripts, ads and analytics also read fingerprinting APIs. Filter
`parsed.calls` on the caller URL to keep one script:

```python
import json
calls = json.load(open("out/inspector/fpcom-win.json"))["parsed"]["calls"]
agent = [c for c in calls if "/web/v4/" in (c["caller"] or [{}])[0].get("url", "")]
```

Comparing which APIs that script calls under two profiles, not only the values,
shows where it branches on what it has already seen.

## Changing what is traced

Edit `third_party/blink/renderer/bindings/phasmid_fingerprint_allowlist.txt`
inside `devtools/fingerprint-trace.patch` (through quilt, as for any patch):

- `Interface.member` traces one member; `Interface.*` traces all of an
  interface's members.
- Members from mixins and partial interfaces carry the including interface's
  name: list `WorkerNavigator.*` beside `Navigator.*`, and
  `WebGL2RenderingContext.*` beside `WebGLRenderingContext.*`.
- An entry that matches nothing, or that names a `[NoAllocDirectCall]` member
  explicitly, fails the build, so a Chromium rebase cannot silently drop
  coverage. Wildcards skip such members without failing.
- A bare `*` traces every Web API. Use it only in a local build; the static
  tests refuse it in the committed list.

The list is an input of the bindings generator, so an edit recompiles only the
generated files whose text changes: minutes on a warm build, not a rebuild of
the tree.

## What it cannot see

- `Date` time zone offsets: V8 builtins that the optimizing compiler inlines,
  so there is no per-call hook.
- `[NoAllocDirectCall]` members: `performance.now`, most Canvas 2D transform
  calls, and the WebGL draw, uniform and vertex-attribute calls. V8's fast path
  skips the traced callback.
- Indexed and named property access (`navigator.plugins[0]`) and the iterators
  of maplike and setlike interfaces (`KeyboardLayoutMap.entries()`).
- `ScriptPromiseProperty` promises (`document.fonts.ready`, `FontFace.loaded`).
- Accessors on cross-origin `Window` objects, and the exception value of a
  synchronous throw (only `threw` is recorded).
- Checks built on JavaScript builtins rather than Web APIs:
  `Function.prototype.toString`, property descriptors, prototype chains,
  `Symbol.toStringTag`. Tampering checks often use these.
- A caller location when no JavaScript frame is on the stack; Intl events carry
  no caller.

## Example: finding what drives a verdict

A detector flags a Windows profile as tampered but passes a Linux profile on
the same binary.

1. Trace the page under both profiles and `diff` the traces, filtered to the
   detector's script. This lists what it reads: here WebGL parameters,
   extensions and precision, WebGPU limits, voices, screen and UA data, and
   one 4x4 WebGL2 `readPixels`.
2. Swap one profile section at a time between the two profiles and rerun.
   Here the verdict follows `gpu.webgl.unmasked_renderer`: a Direct3D 11
   renderer string trips it whatever GPU it names.
3. Trace with every WebGL member listed. The shader sources are in the
   `shaderSource` arguments: a full-screen triangle whose fragment shader
   chains `sin`, `cos`, `pow`, `mod`, `tan`, `exp2` and `log2`, read back as
   4x4 pixels.
4. Replay that probe as a standalone page. Its bytes differ on every
   rasterizer: Direct3D 11, ANGLE on OpenGL and SwiftShader each give their
   own.

The signal is a Direct3D 11 renderer string whose GPU arithmetic is not
Direct3D 11's, which a Linux host cannot produce.
