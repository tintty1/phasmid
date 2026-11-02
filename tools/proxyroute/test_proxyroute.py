"""Tests for proxyroute.

The PAC tests evaluate the generated script in node and skip without it.
The browser test runs only with PHASMID_BINARY=<path to chrome>.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

import proxyroute as pr

HERE = Path(__file__).resolve().parent


# --- parsing ------------------------------------------------------------------


def test_proxy_urls():
    assert pr.parse_proxy("direct") is None
    assert pr.parse_proxy("DIRECT") is None
    p = pr.parse_proxy("http://proxy:8080")
    assert (p.scheme, p.host, p.port, p.authenticated) == ("http", "proxy", 8080, False)
    p = pr.parse_proxy("socks5h://u%40x:p%3Aw%2F@[2001:db8::1]:1080")
    assert (p.scheme, p.host, p.username, p.password) == ("socks5", "2001:db8::1", "u@x", "p:w/")
    assert p.hostport == "[2001:db8::1]:1080"
    assert "p:w" not in p.label and p.label == "socks5://u%40x@[2001:db8::1]:1080"
    p = pr.parse_proxy("https://token@proxy:443")
    assert (p.username, p.password, p.authenticated) == ("token", "", True)


@pytest.mark.parametrize(
    "text",
    ["ftp://p:1", "socks4://p:1", "http://p", "http://p:99999", "http://p:1/path", "http://:pw@p:1", "proxy:8080"],
)
def test_bad_proxy_urls(text):
    with pytest.raises(pr.UsageError):
        pr.parse_proxy(text)


def test_bad_proxy_message_hides_password():
    with pytest.raises(pr.UsageError) as exc:
        pr.parse_proxy("ftp://user:secret@p:1")
    assert "secret" not in str(exc.value)


def test_rule_split():
    rule = pr.parse_rule("*.a.com=http://proxy:1")
    assert (rule.pattern, rule.proxy.host) == ("*.a.com", "proxy")
    # '=' inside a regex and inside a password
    rule = pr.parse_rule("re:^a=b$=socks5://u:p=w@proxy:2")
    assert (rule.regex, rule.proxy.password) == ("^a=b$", "p=w")
    assert pr.parse_rule("*.cdn.com=direct").proxy is None


@pytest.mark.parametrize(
    "text",
    ["*.a.com", "=http://p:1", "https://a.com/x=http://p:1", "a b=http://p:1", "re:=http://p:1",
     "re:(=http://p:1", "re:(?P<x>a)=http://p:1", "re:(?i)a=http://p:1", r"re:\Aa=http://p:1"],
)
def test_bad_rules(text):
    with pytest.raises(pr.UsageError):
        pr.parse_rule(text)


def test_glob():
    import re

    regex = re.compile(pr.glob_regex("*.A.com"), re.I)
    assert regex.match("x.a.com") and regex.match("x.y.a.com") and regex.match("X.A.COM")
    assert not regex.match("a.com") and not regex.match("xa.com") and not regex.match("x.a.com.evil")
    assert re.match(pr.glob_regex("ap?.b.io"), "api.b.io") and not re.match(pr.glob_regex("ap?.b.io"), "ap.b.io")


# --- PAC ----------------------------------------------------------------------

NODE = shutil.which("node")


def run_pac(pac: str, hosts: list[str]) -> list[str]:
    script = (
        pac
        + f"\nprocess.stdout.write(JSON.stringify({json.dumps(hosts)}.map(h => FindProxyForURL('https://' + h + '/', h))));"
    )
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)  # type: ignore[list-item]
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_pac_routes():
    rules = [
        pr.parse_rule("static.a.com=direct"),
        pr.parse_rule("*.a.com=http://proxy-a:8080"),
        pr.parse_rule(r"re:^api\.b\.(io|dev)$=socks5://u:p@proxy-b:1080"),
        pr.parse_rule("*.c.com=https://proxy-c:443"),
    ]
    authed = rules[2].proxy
    pac = pr.build_pac(rules, pr.parse_proxy("http://dflt:3128"), {authed: 40000})
    assert run_pac(pac, ["static.a.com", "x.a.com", "API.b.dev", "api.b.com", "w.c.com", "other.org"]) == [
        "DIRECT",
        "PROXY proxy-a:8080",
        "SOCKS5 127.0.0.1:40000",
        "PROXY dflt:3128",
        "HTTPS proxy-c:443",
        "PROXY dflt:3128",
    ]
    assert run_pac(pr.build_pac(rules[:1], None, {}), ["x.org"]) == ["DIRECT"]


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_pac_fails_closed():
    # (?#...) passes Python's re but throws in JavaScript; check_regex is
    # bypassed here to reach the PAC's catch.
    bad = pr.Rule("re:x", "a(?#c)", pr.parse_proxy("http://p:1"))
    assert run_pac(pr.build_pac([bad], None, {}), ["a", "b"]) == [pr.DEAD_END, pr.DEAD_END]


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_pac_quotes_rule_text():
    # A valid regex holding quotes and U+2028 stays a string literal.
    rule = pr.Rule("x", '^"; return "DIRECT"; //\u2028$', pr.parse_proxy("http://p:1"))
    pac = pr.build_pac([rule], pr.parse_proxy("http://dflt:1"), {})
    assert run_pac(pac, ["h"]) == ["PROXY dflt:1"]


def test_pac_switch_roundtrip():
    pac = pr.build_pac([pr.parse_rule("*.a=direct")], None, {})
    switch = pr.pac_switch(pac)
    prefix = "--proxy-pac-url=data:application/x-javascript-config;base64,"
    assert switch.startswith(prefix)
    assert base64.b64decode(switch[len(prefix):]).decode() == pac


# --- command line ---------------------------------------------------------------


def run_main(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HERE / "proxyroute.py"), *args], capture_output=True, text=True, timeout=30
    )


def test_print_pac():
    out = run_main(["--proxy-rule", "*.a.com=http://p:1", "--print-pac"])
    assert out.returncode == 0 and "FindProxyForURL" in out.stdout


def test_print_pac_refuses_credentials():
    out = run_main(["--proxy-rule", "*.a.com=http://u:secret@p:1", "--print-pac"])
    assert out.returncode == 2 and "relay" in out.stderr and "secret" not in out.stderr


def test_refuses_conflicting_switch():
    out = run_main(["--proxy-default", "http://p:1", "--", "true", "--proxy-server=x:1"])
    assert out.returncode == 2 and "--proxy-server" in out.stderr


def test_needs_something_to_route():
    assert run_main(["--", "true"]).returncode == 2


def test_args_file_and_exec(tmp_path):
    # Without credentials the command is exec'd with the PAC switch appended.
    args = tmp_path / "args"
    args.write_text("--proxy-rule=*.a.com=http://p:1\n")
    out = run_main([f"@{args}", "--", sys.executable, "-c", "import sys; print(sys.argv[-1])"])
    assert out.returncode == 0 and out.stdout.startswith("--proxy-pac-url=data:")


def test_relay_child_exit_code_and_pass_fds(tmp_path):
    # With credentials the launcher stays as parent: it must hand fds 3 and 4
    # through (Playwright's --remote-debugging-pipe) and return the exit code.
    read_fd, write_fd = os.pipe()
    child = (
        "import os, sys; os.write(4, b'pipe-ok'); "
        "assert sys.argv[-1].startswith('--proxy-pac-url='); sys.exit(7)"
    )

    def as_fd_4():  # the launcher sees the pipe's write end as fd 4, /dev/null as 3
        high = os.dup(write_fd)
        os.dup2(os.open(os.devnull, os.O_RDONLY), 3)
        os.dup2(high, 4)

    proc = subprocess.Popen(
        [sys.executable, str(HERE / "proxyroute.py"), "--proxy-default", "http://u:p@127.0.0.1:1",
         "--", sys.executable, "-c", child],
        close_fds=False,  # Popen closes unlisted fds after preexec_fn runs
        preexec_fn=as_fd_4,
    )
    os.close(write_fd)
    assert proc.wait(timeout=30) == 7
    assert os.read(read_fd, 100) == b"pipe-ok"
    os.close(read_fd)


# --- relay against fake upstream proxies -------------------------------------


class FakeUpstreams:
    """An HTTP proxy and a SOCKS5 proxy that both require user/pw and answer
    every tunnel with 'via-<name> <target>' instead of connecting anywhere."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ports: dict[str, int] = {}
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            http = self.loop.run_until_complete(asyncio.start_server(self._http, "127.0.0.1", 0))
            socks = self.loop.run_until_complete(asyncio.start_server(self._socks, "127.0.0.1", 0))
            self.ports = {"http": http.sockets[0].getsockname()[1], "socks5": socks.sockets[0].getsockname()[1]}
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    async def _answer(self, reader, writer, name, target):
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split(b" ")[1].decode()
        body = f"via-{name} {target} {path}".encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()

    async def _http(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        target = head.split(" ")[1]
        if "Proxy-Authorization: Basic " + base64.b64encode(b"alice:s3cr=t").decode() not in head:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await self._answer(reader, writer, "http", target)

    async def _socks(self, reader, writer):
        _, n = await reader.readexactly(2)
        if 2 not in await reader.readexactly(n):
            writer.write(b"\x05\xff")
            writer.close()
            return
        writer.write(b"\x05\x02")
        _, ulen = await reader.readexactly(2)
        user = await reader.readexactly(ulen)
        (plen,) = await reader.readexactly(1)
        password = await reader.readexactly(plen)
        if (user, password) != (b"bob", b"pw"):
            writer.write(b"\x01\x01")
            writer.close()
            return
        writer.write(b"\x01\x00")
        _, _, _, atyp = await reader.readexactly(4)
        host = await pr._read_socks_address(reader, atyp)
        port = int.from_bytes(await reader.readexactly(2), "big")
        writer.write(pr._socks_reply(0))
        await self._answer(reader, writer, "socks5", f"{host}:{port}")


@pytest.fixture(scope="module")
def upstreams():
    return FakeUpstreams()


def socks_get(port: int, host: str, path: str = "/") -> bytes | int:
    """SOCKS5 CONNECT through a relay, then an HTTP GET. Returns the body, or
    the SOCKS reply code when the relay refuses."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00"
        s.sendall(b"\x05\x01\x00" + pr._socks_address(host) + (80).to_bytes(2, "big"))
        reply = s.recv(10)
        if reply[1] != 0:
            return reply[1]
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    return data.split(b"\r\n\r\n", 1)[1]


def start_relay(proxy: pr.Proxy, logs: list[str]) -> pr.Relay:
    relay = pr.Relay(proxy, logs.append)
    pr.run_relays([relay])
    return relay


def test_relay_http_upstream(upstreams):
    logs: list[str] = []
    relay = start_relay(pr.parse_proxy(f"http://alice:s3cr%3Dt@127.0.0.1:{upstreams.ports['http']}"), logs)
    assert socks_get(relay.port, "www.example.test", "/x") == b"via-http www.example.test:80 /x"
    assert socks_get(relay.port, "2001:db8::5") == b"via-http [2001:db8::5]:80 /"
    assert logs == []


def test_relay_socks5_upstream(upstreams):
    logs: list[str] = []
    relay = start_relay(pr.parse_proxy(f"socks5://bob:pw@127.0.0.1:{upstreams.ports['socks5']}"), logs)
    assert socks_get(relay.port, "api.example.test") == b"via-socks5 api.example.test:80 /"
    assert socks_get(relay.port, "192.0.2.7") == b"via-socks5 192.0.2.7:80 /"
    assert logs == []


@pytest.mark.parametrize("scheme,cred,why", [("http", "alice:wrong", "407"), ("socks5", "bob:wrong", "credentials rejected")])
def test_relay_rejected_credentials(upstreams, scheme, cred, why):
    logs: list[str] = []
    relay = start_relay(pr.parse_proxy(f"{scheme}://{cred}@127.0.0.1:{upstreams.ports[scheme]}"), logs)
    assert socks_get(relay.port, "a.test") == 1
    assert len(logs) == 1 and why in logs[0] and "wrong" not in logs[0]


def test_relay_unreachable_upstream():
    logs: list[str] = []
    relay = start_relay(pr.parse_proxy("http://u:p@127.0.0.1:1"), logs)
    assert socks_get(relay.port, "a.test") == 1
    assert len(logs) == 1


# --- the built browser --------------------------------------------------------

BINARY = os.environ.get("PHASMID_BINARY")


@pytest.mark.skipif(not BINARY, reason="set PHASMID_BINARY to the built chrome")
def test_browser_routes(upstreams):
    """Glob to an authenticated HTTP proxy, regex to an authenticated SOCKS5
    proxy, the rest to the default (also through a relay)."""
    with tempfile.TemporaryDirectory() as profile_dir:
        def visit(url: str) -> str:
            out = run_main([
                "--proxy-rule", f"*.a.test=http://alice:s3cr%3Dt@127.0.0.1:{upstreams.ports['http']}",
                "--proxy-rule", rf"re:^api\.b\.(io|test)$=socks5://bob:pw@127.0.0.1:{upstreams.ports['socks5']}",
                "--proxy-default", f"socks5://bob:pw@127.0.0.1:{upstreams.ports['socks5']}",
                "--", BINARY, "--headless", "--no-sandbox", f"--user-data-dir={profile_dir}", "--dump-dom", url,
            ])
            return out.stdout

        assert "via-http www.a.test:80 /p" in visit("http://www.a.test/p")
        assert "via-socks5 api.b.test:80 /" in visit("http://api.b.test/")
        assert "via-socks5 other.test:80 /" in visit("http://other.test/")
