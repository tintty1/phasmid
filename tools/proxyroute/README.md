# proxyroute

Launches Phasmid with requests routed to different proxies by host pattern. A request that matches a rule goes through that rule's proxy. A request that matches no rule goes through `--proxy-default`, or connects directly if no default is given.

```bash
python3 tools/proxyroute/proxyroute.py \
  --proxy-rule '*.a.com=http://user:pass@proxy-a:8080' \
  --proxy-rule 're:^api\.b\.(io|dev)$=socks5://proxy-b:1080' \
  --proxy-rule 'static.c.com=direct' \
  --proxy-default 'http://proxy-c:3128' \
  -- /opt/phasmid/chrome --phasmid-profile=profile.json
```

The tool uses only the standard library (Python 3.12+), so the single file can be copied next to the binary.

## Rules

`--proxy-rule PATTERN=PROXY` can be repeated. Rules are tried in order and the first match wins.

- **PATTERN** is matched against the request **host**, case-insensitively.
  - A glob: `*.a.com`, `api?.b.io`. `*` matches any run of characters, dots included, so `*.a.com` matches `x.a.com` and `x.y.a.com` but not `a.com`.
  - `re:<regex>`: JavaScript regex syntax, unanchored, so add `^…$` yourself. Python-only syntax (`(?P<n>…)`, inline flags, `\A`, `\Z`) and invalid regexes are refused at launch.
  - Only the host can be matched. For HTTPS, Chrome gives a PAC script `scheme://host:port/` and nothing more, so paths are never visible.
- **PROXY** is one of the following:
  - `http://`, `https://` (TLS to the proxy) or `socks5://` (`socks5h://` is an alias), with an optional `user:password@`. Percent-encode `@ : / %` in credentials.
  - `direct`.

`@FILE` reads arguments from a file, one per line (for example `--proxy-rule=*.a.com=http://u:p@h:1`). This keeps credentials out of `ps`.

## How it works

The rules compile into a PAC script, which Chrome receives as `--proxy-pac-url=data:…`. `--print-pac` prints the script. The script runs in Chrome's proxy resolver and is not visible to pages.

**It fails closed.** If a PAC script throws or doesn't parse, Chrome connects **directly**. If a proxy is unreachable, the request fails, and Chrome doesn't fall back to direct. The generated script therefore keeps all its logic inside `try`, and the `catch` returns an unreachable proxy (`127.0.0.1:1`). Rule text is embedded as JSON literals, so no pattern can break the script's syntax.

**Authenticated proxies go through a relay.** Chrome can't send proxy credentials on its own: it ignores `user:pass@` in proxy strings and would prompt for them, or wait for CDP `Fetch.authRequired`. For each proxy with a user name, proxyroute opens a SOCKS5 listener on `127.0.0.1` without auth. The PAC points that proxy's hosts at the listener, and the relay opens the upstream connection with the credentials:

- HTTP or HTTPS upstream: `CONNECT` with Basic auth.
- SOCKS5 upstream: username/password auth (RFC 1929).

Chrome's SOCKS5 always sends the host name, so DNS is resolved by the upstream proxy. Plain-HTTP requests also reach an HTTP upstream as `CONNECT host:80` tunnels, so the proxy can't add `Via` or `X-Forwarded-For` headers. Its `CONNECT` policy has to allow port 80, as most commercial proxies do. Failures are logged to stderr without the password, for example `proxyroute: http://alice@proxy:8080 -> example.com:443: credentials rejected (407)`.

**Processes.** Without authenticated proxies, proxyroute `exec`s Chrome and is gone. With them, it stays as Chrome's parent:

- It forwards SIGINT, SIGTERM and SIGHUP to Chrome.
- It passes fds 3 and 4 through (Playwright's `--remote-debugging-pipe`).
- It exits with Chrome's exit code.
- Chrome gets SIGTERM if proxyroute dies (`PR_SET_PDEATHSIG`).

Any local user can use the relay listener on loopback, together with its credentials, while the browser runs.

## Playwright

Point `executable_path` at a wrapper, and don't use Playwright's own `proxy` option. It adds `--proxy-server`, which proxyroute refuses.

```sh
#!/bin/sh
exec python3 /opt/phasmid/proxyroute.py @/etc/phasmid/proxy-args -- /opt/phasmid/chrome "$@"
```

## One exit per profile

A profile's location group comes from one exit IP (`genprofile --exit-ip`): `network.webrtc.public_ip`, the timezone, geolocation and languages. A detector sees the address its own requests arrive from. If the page and its detection scripts' API hosts leave through different proxies, or through a proxy other than the one the profile was generated for, the IP no longer agrees with the timezone or the WebRTC candidate. Send the site, and every host that fingerprints or checks for bots, through the profile's exit. Use other proxies, or `direct`, only for traffic where the source address doesn't matter, such as static assets and CDNs.

WebRTC UDP doesn't go through proxies. Rules don't change that.

## Tests

```bash
cd tools/proxyroute && uv run pytest -q
PHASMID_BINARY=/path/to/chrome uv run pytest -q -k browser   # routes real browser requests
```

The PAC tests evaluate the generated script in node and are skipped without it.
