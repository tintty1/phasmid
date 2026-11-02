"""Guard for patches/phasmid/network/webrtc.patch.

Offline: a STUN server in this process, on 127.0.0.1, answers every Binding
Request with a Binding Success Response whose XOR-MAPPED-ADDRESS is a fixed
fake public address (203.0.113.9, the request's source port) and a
FINGERPRINT. A page creates an RTCPeerConnection with that server, in the
main frame and in a same-origin iframe, and collects candidates.

  0. precondition, no profile: a srflx candidate carries 203.0.113.9, so the
     fake server and the gathering path work (else "vacuous");
  1. network.webrtc.public_ip=198.51.100.7, policy "default": every srflx
     candidate carries 198.51.100.7 in onicecandidate and in the SDP, host
     candidates still exist and are mDNS names, never raw addresses, and
     203.0.113.9 appears nowhere;
  2. policy "default_public_interface_only": srflx still carries 198.51.100.7
     and 203.0.113.9 appears nowhere;
  3. policy "disable_non_proxied_udp" (no proxy): no candidates at all, as in
     stock Chrome;
  4. public_ip of the other family (2001:db8::7): the IPv4 STUN answer is
     dropped, so there is no srflx candidate, and host candidates remain.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import zlib
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from helpers import GuardFailure, finish, load_profile, parse_args

FAKE_IP = "203.0.113.9"
PROFILE_IP = "198.51.100.7"
PROFILE_IP_OTHER_FAMILY = "2001:db8::7"
MAGIC = 0x2112A442

GATHER_JS = """async (stun) => {
  const pc = new RTCPeerConnection({ iceServers: [{ urls: stun }] });
  pc.createDataChannel("g");
  const cands = [];
  const done = new Promise((res) => {
    pc.onicecandidate = (e) => {
      if (!e.candidate) return res();
      cands.push({ candidate: e.candidate.candidate, type: e.candidate.type, address: e.candidate.address });
    };
    setTimeout(res, 4000);
  });
  await pc.setLocalDescription(await pc.createOffer());
  await done;
  const sdp = pc.localDescription ? pc.localDescription.sdp : "";
  pc.close();
  return { cands, sdp: sdp.split("\\r\\n").filter((l) => l.startsWith("a=candidate")) };
}"""


class StunServer:
    """Answers Binding Requests with FAKE_IP as the mapped address."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "StunServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, (_, port) = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 20:
                continue
            msg_type, _, cookie = struct.unpack("!HHI", data[:8])
            if msg_type != 0x0001 or cookie != MAGIC:
                continue
            self.requests += 1
            txid = data[8:20]
            xaddr = struct.unpack("!I", socket.inet_aton(FAKE_IP))[0] ^ MAGIC
            xor_mapped = struct.pack("!HHBBHI", 0x0020, 8, 0, 0x01, port ^ (MAGIC >> 16), xaddr)
            # Length covers XOR-MAPPED-ADDRESS (12) and FINGERPRINT (8).
            head = struct.pack("!HHI", 0x0101, len(xor_mapped) + 8, MAGIC) + txid
            body = head + xor_mapped
            crc = (zlib.crc32(body) ^ 0x5354554E) & 0xFFFFFFFF
            self.sock.sendto(body + struct.pack("!HHI", 0x8028, 4, crc), ("127.0.0.1", port))


def gather(binary: str, profile: Path | None, stun: str) -> dict[str, dict]:
    """Candidates and SDP candidate lines from the main frame and an iframe."""
    with sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=True, profile=profile)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content('<iframe srcdoc="<p>frame</p>"></iframe>')
            page.wait_for_function("() => document.querySelector('iframe').contentDocument?.readyState === 'complete'")
            frame = next(f for f in page.frames if f != page.main_frame)
            return {"main": page.evaluate(GATHER_JS, stun), "iframe": frame.evaluate(GATHER_JS, stun)}
        finally:
            launch.stop()


def sdp_fields(line: str) -> tuple[str, str]:
    """(address, type) of an a=candidate line."""
    parts = line.split()
    return parts[4], parts[7]


def srflx(result: dict) -> tuple[list[str], list[str]]:
    js = [c["address"] for c in result["cands"] if c["type"] == "srflx"]
    sdp = [addr for addr, typ in map(sdp_fields, result["sdp"]) if typ == "srflx"]
    return js, sdp


def expect_srflx(results: dict[str, dict], want: str, label: str) -> None:
    for ctx, r in results.items():
        js, sdp = srflx(r)
        if not js or any(a != want for a in js):
            raise GuardFailure(f"{label}, {ctx}: onicecandidate srflx addresses {js}, want only {want}")
        if not sdp or any(a != want for a in sdp):
            raise GuardFailure(f"{label}, {ctx}: SDP srflx addresses {sdp}, want only {want}")
        leaked = [c["candidate"] for c in r["cands"] if FAKE_IP in c["candidate"]] + [line for line in r["sdp"] if FAKE_IP in line]
        if leaked:
            raise GuardFailure(f"{label}, {ctx}: the real mapped address {FAKE_IP} leaks: {leaked}")
    print(f"PASS: {label}: srflx is {want} in onicecandidate and SDP, {FAKE_IP} nowhere ({', '.join(results)})")


def with_webrtc(profile: dict, work: Path, name: str, **webrtc) -> Path:
    p = dict(profile)
    p["network"] = dict(profile.get("network", {}), webrtc=webrtc)
    path = work / f"{name}.json"
    path.write_text(json.dumps(p))
    return path


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)

    with StunServer() as server:
        stun = f"stun:127.0.0.1:{server.port}"

        plain = gather(args.binary, None, stun)
        for ctx, r in plain.items():
            js, sdp = srflx(r)
            if FAKE_IP not in js or FAKE_IP not in sdp:
                raise GuardFailure(
                    f"vacuous: without a profile, {ctx} shows no srflx candidate {FAKE_IP} "
                    f"(STUN requests answered: {server.requests}, candidates: {r['cands']})"
                )
        print(f"PASS: precondition: without a profile srflx is {FAKE_IP} in main and iframe")

        r = gather(args.binary, with_webrtc(profile, work, "default", ip_handling_policy="default", public_ip=PROFILE_IP), stun)
        expect_srflx(r, PROFILE_IP, 'policy "default"')
        for ctx, res in r.items():
            if not any(c["type"] == "host" for c in res["cands"]):
                raise GuardFailure(f'policy "default", {ctx}: no host candidate; WebRTC must keep gathering')
        print('PASS: policy "default": host candidates still gathered')
        for ctx, res in r.items():
            raw = [c["address"] for c in res["cands"] if c["type"] == "host" and not (c["address"] or "").endswith(".local")]
            raw += [line for line in res["sdp"] if " typ host " in line and ".local " not in line]
            if raw:
                raise GuardFailure(f'policy "default", {ctx}: host candidates expose raw addresses {raw}; stock Chrome names them by mDNS')
        print('PASS: policy "default": every host candidate is an mDNS name, as in stock Chrome')

        r = gather(
            args.binary,
            with_webrtc(profile, work, "public-interface", ip_handling_policy="default_public_interface_only", public_ip=PROFILE_IP),
            stun,
        )
        expect_srflx(r, PROFILE_IP, 'policy "default_public_interface_only"')

        r = gather(
            args.binary,
            with_webrtc(profile, work, "no-udp", ip_handling_policy="disable_non_proxied_udp", public_ip=PROFILE_IP),
            stun,
        )
        for ctx, res in r.items():
            if res["cands"] or res["sdp"]:
                raise GuardFailure(f'policy "disable_non_proxied_udp", {ctx}: expected no candidates, got {res["cands"]} / {res["sdp"]}')
        print('PASS: policy "disable_non_proxied_udp": no candidates without a proxy')

        r = gather(
            args.binary,
            with_webrtc(profile, work, "other-family", ip_handling_policy="default", public_ip=PROFILE_IP_OTHER_FAMILY),
            stun,
        )
        for ctx, res in r.items():
            js, sdp = srflx(res)
            if js or sdp:
                raise GuardFailure(f"public_ip {PROFILE_IP_OTHER_FAMILY} with an IPv4 STUN answer, {ctx}: expected no srflx, got {js} / {sdp}")
            if not any(c["type"] == "host" for c in res["cands"]):
                raise GuardFailure(f"public_ip {PROFILE_IP_OTHER_FAMILY}, {ctx}: no host candidate; WebRTC must keep gathering")
        print(f"PASS: public_ip {PROFILE_IP_OTHER_FAMILY}: the IPv4 STUN answer is dropped, host candidates remain")


finish(main)
