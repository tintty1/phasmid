#!/usr/bin/env python3
"""Launch Phasmid with requests routed to different proxies by host pattern.

    proxyroute.py --proxy-rule '*.a.com=http://user:pass@proxy-a:8080' \\
                  --proxy-rule 're:^api\\.b\\.(io|dev)$=socks5://proxy-b:1080' \\
                  --proxy-default http://proxy-c:3128 \\
                  -- /opt/phasmid/chrome --phasmid-profile=profile.json

The rules compile into a PAC script that Chrome receives through
--proxy-pac-url as a data: URL. The first matching rule picks the proxy; a
host no rule matches uses --proxy-default, or connects directly without it.

Chrome cannot send proxy credentials on its own, so every proxy that has a
user name gets a local relay: a SOCKS5 listener on 127.0.0.1 that opens the
upstream connection with the credentials (HTTP CONNECT with Basic auth, or
SOCKS5 username/password). The PAC sends that proxy's hosts to the relay.
Without authenticated proxies this program execs Chrome and is gone; with
them it stays as Chrome's parent until Chrome exits.

Standard library only, so the file can be copied next to the binary.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

# PAC directive per proxy scheme. socks5h is accepted as an alias: Chrome's
# SOCKS5 always sends the host name, so DNS is always resolved by the proxy.
PAC_KEYWORD = {"http": "PROXY", "https": "HTTPS", "socks5": "SOCKS5"}
SCHEME_ALIASES = {"socks5h": "socks5"}

# Where a request goes when the PAC script fails. An exception in
# FindProxyForURL (or a script that does not parse) makes Chrome connect
# directly, which would bypass every proxy; an unreachable proxy makes the
# request fail instead, and Chrome does not fall back to DIRECT after it.
DEAD_END = "PROXY 127.0.0.1:1"

# Switches that set Chrome's proxy configuration; the PAC replaces them all.
CHROME_PROXY_SWITCHES = (
    "--proxy-server",
    "--proxy-pac-url",
    "--no-proxy-server",
    "--proxy-auto-detect",
    "--proxy-bypass-list",
)

# A rule is PATTERN=PROXY. The pattern may itself contain '=' (a regex) and
# so may a password, so the split is at the first '=' that starts a proxy URL.
RULE_SPLIT = re.compile(r"=(?=(?:https?|socks5h?)://|direct$)", re.IGNORECASE)

CONNECT_TIMEOUT = 30.0
PIPE_CHUNK = 64 * 1024


class UsageError(Exception):
    """A rule, proxy or command the user has to fix."""


class UpstreamError(Exception):
    """The upstream proxy refused or failed the connection."""


@dataclass(frozen=True)
class Proxy:
    scheme: str  # http, https or socks5
    host: str
    port: int
    username: str | None = None
    password: str = ""

    @property
    def authenticated(self) -> bool:
        return self.username is not None

    @property
    def hostport(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def label(self) -> str:
        """The proxy URL without the password, for messages."""
        user = f"{quote(self.username, safe='')}@" if self.username is not None else ""
        return f"{self.scheme}://{user}{self.hostport}"


@dataclass(frozen=True)
class Rule:
    pattern: str  # as given
    regex: str  # matched case-insensitively against the request host
    proxy: Proxy | None  # None is DIRECT


def parse_proxy(text: str) -> Proxy | None:
    """Parse a proxy URL, or 'direct'. Credentials are percent-decoded."""
    if text.lower() == "direct":
        return None
    parts = urlsplit(text)
    scheme = SCHEME_ALIASES.get(parts.scheme.lower(), parts.scheme.lower())
    shown = _redact(text)
    if scheme not in PAC_KEYWORD:
        raise UsageError(f"{shown}: scheme must be http, https, socks5 or the word direct")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise UsageError(f"{shown}: a proxy URL has no path, query or fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise UsageError(f"{shown}: {exc}") from None
    if not parts.hostname or port is None:
        raise UsageError(f"{shown}: needs a host and a port")
    username = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else ""
    if username == "":
        raise UsageError(f"{shown}: empty user name")
    if username is not None and scheme == "socks5":
        # RFC 1929 length fields are one byte.
        if len(username.encode()) > 255 or len(password.encode()) > 255:
            raise UsageError(f"{shown}: SOCKS5 user name and password are limited to 255 bytes")
    return Proxy(scheme, parts.hostname, port, username, password)


def _redact(text: str) -> str:
    return re.sub(r"(//[^:/@]*:)[^@/]*@", r"\1***@", text)


_HOST_CHARS = re.compile(r"[a-z0-9.\-_*?:\[\]]+")


def glob_regex(pattern: str) -> str:
    """Translate a host glob to an anchored regex.

    '*' matches any run of characters, dots included ('*.a.com' matches
    'x.a.com' and 'x.y.a.com', not 'a.com'); '?' matches one character.
    This is shExpMatch's meaning.
    """
    glob = pattern.lower()
    if "://" in glob or "/" in glob:
        raise UsageError(f"{pattern}: patterns match the host only, not a URL")
    if not _HOST_CHARS.fullmatch(glob):
        raise UsageError(f"{pattern}: not a host pattern")
    out = []
    for ch in glob:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch.isalnum() or ch in "-_:":
            out.append(ch)
        else:
            out.append("\\" + ch)
    return "^" + "".join(out) + "$"


# Python-only regex syntax that would throw in the PAC's JavaScript RegExp.
_PYTHON_ONLY = (
    (re.compile(r"\(\?P[<=>]"), "named groups are (?<name>...) in JavaScript"),
    (re.compile(r"\(\?[aiLmsux]+\)"), "inline flags are not JavaScript; matching is already case-insensitive"),
    (re.compile(r"\\[AZ]"), "\\A and \\Z are not JavaScript; use ^ and $"),
)


def check_regex(source: str, pattern: str) -> None:
    if not source:
        raise UsageError(f"{pattern}: empty regex")
    for bad, why in _PYTHON_ONLY:
        if bad.search(source):
            raise UsageError(f"{pattern}: {why}")
    try:
        re.compile(source, re.IGNORECASE)
    except re.error as exc:
        raise UsageError(f"{pattern}: invalid regex: {exc}") from None


def parse_rule(text: str) -> Rule:
    match = RULE_SPLIT.search(text)
    if not match:
        raise UsageError(f"{_redact(text)}: expected PATTERN=PROXY (PROXY is a URL or direct)")
    pattern, proxy_text = text[: match.start()], text[match.end() :]
    if not pattern:
        raise UsageError(f"{_redact(text)}: empty pattern")
    if pattern.startswith("re:"):
        regex = pattern[3:]
        check_regex(regex, pattern)
    else:
        regex = glob_regex(pattern)
    return Rule(pattern, regex, parse_proxy(proxy_text))


def pac_target(proxy: Proxy | None, relay_ports: dict[Proxy, int]) -> str:
    if proxy is None:
        return "DIRECT"
    if proxy.authenticated:
        return f"SOCKS5 127.0.0.1:{relay_ports[proxy]}"
    return f"{PAC_KEYWORD[proxy.scheme]} {proxy.hostport}"


def build_pac(rules: list[Rule], default: Proxy | None, relay_ports: dict[Proxy, int]) -> str:
    """The PAC script. Everything runs inside try, so it fails closed."""
    table = [[rule.regex, pac_target(rule.proxy, relay_ports)] for rule in rules]
    # json.dumps output is a valid JavaScript literal (ensure_ascii escapes
    # U+2028/U+2029 too), so no rule text can break the script's syntax.
    return (
        "// Generated by tools/proxyroute. An exception in a PAC script makes\n"
        "// Chrome connect directly, so every path returns a proxy and the catch\n"
        "// returns an unreachable one.\n"
        "function FindProxyForURL(url, host) {\n"
        "  try {\n"
        f"    var rules = {json.dumps(table)};\n"
        "    for (var i = 0; i < rules.length; i++) {\n"
        '      if (new RegExp(rules[i][0], "i").test(host)) return rules[i][1];\n'
        "    }\n"
        f"    return {json.dumps(pac_target(default, relay_ports))};\n"
        "  } catch (e) {\n"
        f"    return {json.dumps(DEAD_END)};\n"
        "  }\n"
        "}\n"
    )


def pac_switch(pac: str) -> str:
    encoded = base64.b64encode(pac.encode()).decode()
    return f"--proxy-pac-url=data:application/x-javascript-config;base64,{encoded}"


# --- relay ------------------------------------------------------------------


async def socks5_accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[str, int]:
    """Server side of a SOCKS5 handshake without auth. Returns the target."""
    version, nmethods = await reader.readexactly(2)
    if version != 5:
        raise UpstreamError(f"client spoke SOCKS version {version}")
    methods = await reader.readexactly(nmethods)
    if 0 not in methods:
        writer.write(b"\x05\xff")
        raise UpstreamError("client offered no no-auth method")
    writer.write(b"\x05\x00")
    _, command, _, atyp = await reader.readexactly(4)
    host = await _read_socks_address(reader, atyp)
    port = int.from_bytes(await reader.readexactly(2), "big")
    if command != 1:
        writer.write(_socks_reply(7))
        raise UpstreamError(f"client sent SOCKS command {command}, only CONNECT is relayed")
    return host, port


async def _read_socks_address(reader: asyncio.StreamReader, atyp: int) -> str:
    if atyp == 1:
        return socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
    if atyp == 4:
        return socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    if atyp == 3:
        (length,) = await reader.readexactly(1)
        return (await reader.readexactly(length)).decode("ascii")
    raise UpstreamError(f"unknown SOCKS address type {atyp}")


def _socks_reply(code: int) -> bytes:
    return bytes([5, code, 0, 1]) + bytes(6)


def _socks_address(host: str) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode("ascii")
        return b"\x03" + bytes([len(name)]) + name
    return (b"\x01" if ip.version == 4 else b"\x04") + ip.packed


async def open_upstream(proxy: Proxy, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to host:port through the authenticated upstream proxy."""
    tls = ssl.create_default_context() if proxy.scheme == "https" else None
    reader, writer = await asyncio.open_connection(
        proxy.host, proxy.port, ssl=tls, server_hostname=proxy.host if tls else None
    )
    try:
        if proxy.scheme == "socks5":
            await _socks5_connect(reader, writer, proxy, host, port)
        else:
            await _http_connect(reader, writer, proxy, host, port)
    except BaseException:
        writer.close()
        raise
    return reader, writer


async def _http_connect(reader, writer, proxy: Proxy, host: str, port: int) -> None:
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    credentials = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
    writer.write(
        f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
        f"Proxy-Authorization: Basic {credentials}\r\n\r\n".encode()
    )
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
    fields = status_line.split(" ", 2)
    if len(fields) < 2 or not fields[0].startswith("HTTP/") or not fields[1].isdigit():
        raise UpstreamError(f"not an HTTP proxy answer: {status_line!r}")
    code = int(fields[1])
    if code == 407:
        raise UpstreamError("credentials rejected (407)")
    if not 200 <= code < 300:
        raise UpstreamError(f"CONNECT refused: {status_line}")


async def _socks5_connect(reader, writer, proxy: Proxy, host: str, port: int) -> None:
    writer.write(b"\x05\x01\x02")  # one method: username/password
    if await reader.readexactly(2) != b"\x05\x02":
        raise UpstreamError("proxy does not accept username/password auth")
    user, password = proxy.username.encode(), proxy.password.encode()  # type: ignore[union-attr]
    writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
    if (await reader.readexactly(2))[1] != 0:
        raise UpstreamError("credentials rejected")
    writer.write(b"\x05\x01\x00" + _socks_address(host) + port.to_bytes(2, "big"))
    await writer.drain()
    _, reply, _, atyp = await reader.readexactly(4)
    if reply != 0:
        raise UpstreamError(f"CONNECT refused (SOCKS5 reply {reply})")
    await _read_socks_address(reader, atyp)
    await reader.readexactly(2)


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(PIPE_CHUNK):
            dst.write(data)
            await dst.drain()
        if dst.can_write_eof():
            dst.write_eof()
        else:
            dst.close()
    except (ConnectionError, OSError):
        dst.close()


class Relay:
    """SOCKS5 on 127.0.0.1 without auth, forwarded through one upstream."""

    def __init__(self, upstream: Proxy, log: Callable[[str], None]):
        self.upstream = upstream
        self.log = log
        # Bound and listening now, so the port can go into the PAC and
        # connections queue before the event loop starts.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.sock.setblocking(False)
        self.port: int = self.sock.getsockname()[1]
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, sock=self.sock)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target = "?"
        try:
            host, port = await socks5_accept(reader, writer)
            target = f"{host}:{port}"
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    open_upstream(self.upstream, host, port), CONNECT_TIMEOUT
                )
            except (OSError, UpstreamError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as exc:
                self.log(f"proxyroute: {self.upstream.label} -> {target}: {_describe(exc)}")
                writer.write(_socks_reply(1))
                await writer.drain()
                return
            writer.write(_socks_reply(0))
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
            up_writer.close()
        except (OSError, UpstreamError, asyncio.IncompleteReadError, UnicodeDecodeError) as exc:
            self.log(f"proxyroute: relay for {self.upstream.label} -> {target}: {_describe(exc)}")
        finally:
            writer.close()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return f"no answer within {CONNECT_TIMEOUT:.0f} s"
    if isinstance(exc, asyncio.IncompleteReadError):
        return "proxy closed the connection"
    return str(exc) or type(exc).__name__


def run_relays(relays: list[Relay]) -> threading.Thread:
    loop = asyncio.new_event_loop()

    def serve() -> None:
        asyncio.set_event_loop(loop)
        for relay in relays:
            loop.run_until_complete(relay.start())
        loop.run_forever()

    thread = threading.Thread(target=serve, name="proxyroute-relays", daemon=True)
    thread.start()
    return thread


# --- launcher ---------------------------------------------------------------


def _die_with_parent(parent: int) -> Callable[[], None]:
    def setup() -> None:
        # PR_SET_PDEATHSIG: Chrome gets SIGTERM if the relays' process dies,
        # rather than living on with no route to its proxies.
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)
        if os.getppid() != parent:
            os._exit(1)

    return setup


def _open_fds(candidates: tuple[int, ...]) -> list[int]:
    open_fds = []
    for fd in candidates:
        try:
            os.fstat(fd)
        except OSError:
            continue
        open_fds.append(fd)
    return open_fds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxyroute",
        fromfile_prefix_chars="@",
        description="Launch Phasmid with requests routed to proxies by host pattern.",
        epilog=(
            "PATTERN is a host glob (*.a.com) or re:<JavaScript regex>, both case-insensitive "
            "and matched against the host only. PROXY is http://, https:// or socks5:// with "
            "an optional user:password@ (percent-encode special characters), or the word "
            "direct. @FILE reads arguments from FILE, one per line, which keeps credentials "
            "out of the process list."
        ),
    )
    parser.add_argument(
        "--proxy-rule",
        action="append",
        default=[],
        metavar="PATTERN=PROXY",
        help="route hosts matching PATTERN to PROXY; repeatable, the first match wins",
    )
    parser.add_argument(
        "--proxy-default",
        metavar="PROXY",
        help="proxy for hosts no rule matches (default: direct)",
    )
    parser.add_argument(
        "--print-pac",
        action="store_true",
        help="print the PAC script and exit (only without authenticated proxies)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- CHROME [ARGS...]")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Playwright's --remote-debugging-pipe talks to Chrome on fds 3 and 4.
    # Recorded before anything here opens a file or socket that could take
    # those numbers.
    inherited = _open_fds((3, 4))
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        rules = [parse_rule(text) for text in args.proxy_rule]
        default = parse_proxy(args.proxy_default) if args.proxy_default else None
    except UsageError as exc:
        parser.error(str(exc))
    if not rules and default is None:
        parser.error("nothing to route: give --proxy-rule or --proxy-default")

    authenticated = list(dict.fromkeys(p for p in [*(r.proxy for r in rules), default] if p and p.authenticated))

    if args.print_pac:
        if authenticated:
            parser.error(
                "--print-pac: authenticated proxies need the relay; "
                f"run with a command ({', '.join(p.label for p in authenticated)})"
            )
        sys.stdout.write(build_pac(rules, default, {}))
        return 0

    if not command:
        parser.error("missing command: -- CHROME [ARGS...]")
    for arg in command[1:]:
        for switch in CHROME_PROXY_SWITCHES:
            if arg == switch or arg.startswith(switch + "="):
                parser.error(f"{switch} in the command conflicts with the proxy rules")

    def log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    try:
        relays = [Relay(proxy, log) for proxy in authenticated]
    except OSError as exc:
        parser.error(f"cannot open a relay port: {exc}")
    pac = build_pac(rules, default, {relay.upstream: relay.port for relay in relays})
    chrome = [*command, pac_switch(pac)]

    if not relays:
        try:
            os.execvp(chrome[0], chrome)
        except OSError as exc:
            parser.error(f"{chrome[0]}: {exc.strerror}")

    try:
        # No thread runs yet, so preexec_fn is safe here.
        child = subprocess.Popen(chrome, pass_fds=inherited, preexec_fn=_die_with_parent(os.getpid()))
    except OSError as exc:
        parser.error(f"{chrome[0]}: {exc.strerror}")
    for fd in inherited:
        os.close(fd)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, _: child.send_signal(signum))
    run_relays(relays)
    code = child.wait()
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(main())
