"""Guard for patches/phasmid/graphics/hardware-decode.patch.

gpu.hardware_decode, and media.hevc with it, is what pages see, whatever
VA-API driver the host has or lacks, headless and headful alike.

  1. a profile that claims no hardware decode gets none. Observable only on
     a host whose own hardware decode is visible (a VA-API driver Chromium
     accepts, Intel or AMD with Mesa; Xvfb runs on software GL, which
     never decodes in hardware, and headless takes --enable-gpu without a
     profile, the host GPU a profile run renders on); elsewhere this check is
     reported as not observable and the guard goes on. Precondition, no
     profile: main-thread decodingInfo powerEfficient is true for at least
     one of H.264, HEVC, VP9 and AV1, or HEVC is supported at all. With the
     profile: powerEfficient is false for every codec in the main frame and
     both iframes, HEVC is unsupported in canPlayType,
     MediaSource.isTypeSupported (also in a dedicated worker) and
     decodingInfo, and the workers' powerEfficient, which stock Chrome takes
     from the decode statistics database, equals the plain run's;
  2. a profile that claims hardware H.264, HEVC and VP9 but not AV1, with
     media.hevc (the win11-uhd600 example's claims), as a linux and as a
     windows profile: it starts with the gpu.hardware_decode and media.hevc
     warnings; main-thread and
     iframe powerEfficient is the claim per codec; HEVC is "probably" in
     canPlayType and supported by MediaSource.isTypeSupported (also in a
     dedicated worker) and by decodingInfo in every context, as Windows
     Chrome reports it; the workers' powerEfficient is again the database's:
     the plain run's for a codec whose support the claim leaves alone, and
     for HEVC, which the claim makes supported, the database's answer for a
     supported codec it has no statistics for, which is the plain run's
     H.264 value (true), as Windows Chrome reports worker HEVC.
     Precondition: the plain run differs from the claim somewhere (HEVC
     support or a codec's powerEfficient), so the check is not vacuous.
"""

from __future__ import annotations

import json
from pathlib import Path

from _gpu import host_gpu_args
from consistency.compare import _get
from helpers import (ALL_CONTEXTS, WINDOW_CONTEXTS, GuardFailure, dump, expect_everywhere, expect_warned, finish, load_profile, parse_args,
                     start_and_exit, windows_variant)

HEVC = ('video/mp4; codecs="hev1.1.6.L93.B0"', 'video/mp4; codecs="hvc1.1.6.L93.B0"')
DECODE = {"h264": "h264_1080p", "hevc": "hevc_1080p", "vp9": "vp9_4k", "av1": "av1_1080p"}
WORKERS = ("worker", "shared_worker", "service_worker")
CLAIM = {"h264": True, "hevc": True, "vp9": True, "av1": False}


def expect_workers_keep_database(d: dict, plain: dict) -> None:
    """Workers' powerEfficient is the decode statistics database's answer,
    as in stock: false for an unsupported codec, and for a supported one
    what the database says without statistics, which the plain run shows for
    H.264 (supported on every host)."""
    for ctx in WORKERS:
        if _get(plain["contexts"][ctx], "codecs.decodingInfo.h264_1080p.supported") is not True:
            raise GuardFailure(f"vacuous: without a profile {ctx} reports H.264 unsupported, so the database's answer for a supported codec is unknown")
    for config in DECODE.values():
        for ctx in WORKERS:
            supported = _get(d["contexts"][ctx], f"codecs.decodingInfo.{config}.supported")
            plain_supported = _get(plain["contexts"][ctx], f"codecs.decodingInfo.{config}.supported")
            if supported == plain_supported:
                want = _get(plain["contexts"][ctx], f"codecs.decodingInfo.{config}.powerEfficient")
                label = f"{ctx} decodingInfo {config} powerEfficient keeps stock's database value"
            elif supported is True:
                want = _get(plain["contexts"][ctx], "codecs.decodingInfo.h264_1080p.powerEfficient")
                label = f"{ctx} decodingInfo {config} (now supported) powerEfficient is the database's value for a supported codec, as H.264's"
            else:
                want = False
                label = f"{ctx} decodingInfo {config} (now unsupported) powerEfficient"
            expect_everywhere(d, f"codecs.decodingInfo.{config}.powerEfficient", want, (ctx,), label=label)


def check_no_decode(args, profile: dict, plain: dict) -> None:
    main_ctx = plain["contexts"]["main"]
    host = {codec: _get(main_ctx, f"codecs.decodingInfo.{config}.powerEfficient") for codec, config in DECODE.items()}
    host_hevc = _get(main_ctx, ("codecs", "canPlayType", HEVC[0]))
    if not any(v is True for v in host.values()) and not host_hevc:
        mode = "headful" if args.headful else "headless"
        print(f"NOT OBSERVABLE: check 1: the host shows no hardware video decode {mode} (powerEfficient {host}, HEVC canPlayType "
              f"{host_hevc!r}); a profile without hardware decode differs from the host only on a VA-API host")
        return
    print(f"PASS: precondition: the host decodes in hardware without a profile (powerEfficient {host}, HEVC {host_hevc!r})")
    none = json.loads(json.dumps(profile))
    none["gpu"]["hardware_decode"] = {codec: False for codec in DECODE}
    none["media"]["hevc"] = False
    path = Path(args.work_dir) / "no-decode.json"
    path.write_text(json.dumps(none))
    d = dump(args.binary, str(path), headless=not args.headful)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    for codec, config in DECODE.items():
        expect_everywhere(d, f"codecs.decodingInfo.{config}.powerEfficient", False, WINDOW_CONTEXTS,
                          label=f"gpu.hardware_decode.{codec} false (decodingInfo {config} powerEfficient)")
    for mime in HEVC:
        expect_everywhere(d, ("codecs", "canPlayType", mime), "", WINDOW_CONTEXTS, label=f"canPlayType({mime})")
        expect_everywhere(d, ("codecs", "mediaSourceIsTypeSupported", mime), False, (*WINDOW_CONTEXTS, "worker"), label=f"MediaSource.isTypeSupported({mime})")
    expect_everywhere(d, "codecs.decodingInfo.hevc_1080p.supported", False, ALL_CONTEXTS)
    expect_workers_keep_database(d, plain)


def check_claim(args, base: dict, plain: dict, name: str) -> None:
    main_ctx = plain["contexts"]["main"]
    host = {codec: _get(main_ctx, f"codecs.decodingInfo.{config}.powerEfficient") for codec, config in DECODE.items()}
    host_hevc = _get(main_ctx, ("codecs", "canPlayType", HEVC[0]))
    if host == CLAIM and host_hevc == "probably":
        raise GuardFailure(f"vacuous: the host already reports the claim {CLAIM} and HEVC {host_hevc!r}")
    claimed = json.loads(json.dumps(base))
    claimed["gpu"]["hardware_decode"] = dict(CLAIM)
    claimed["media"]["hevc"] = True
    work = Path(args.work_dir)
    r = start_and_exit(args.binary, claimed, work, name)
    expect_warned(r, name, "gpu.hardware_decode", "h264, hevc, vp9")
    expect_warned(r, name, "media.hevc", "HEVC playback fails")
    d = dump(args.binary, str(work / f"{name}.json"), headless=not args.headful)
    if d["context_errors"]:
        raise GuardFailure(f"{name}: contexts failed: {d['context_errors']}")
    for codec, config in DECODE.items():
        expect_everywhere(d, f"codecs.decodingInfo.{config}.powerEfficient", CLAIM[codec], WINDOW_CONTEXTS,
                          label=f"{name}: gpu.hardware_decode.{codec}={CLAIM[codec]} (decodingInfo {config} powerEfficient)")
    for mime in HEVC:
        expect_everywhere(d, ("codecs", "canPlayType", mime), "probably", WINDOW_CONTEXTS, label=f"{name}: canPlayType({mime})")
        expect_everywhere(d, ("codecs", "mediaSourceIsTypeSupported", mime), True, (*WINDOW_CONTEXTS, "worker"),
                          label=f"{name}: MediaSource.isTypeSupported({mime})")
    expect_everywhere(d, "codecs.decodingInfo.hevc_1080p.supported", True, ALL_CONTEXTS, label=f"{name}: decodingInfo hevc_1080p supported")
    expect_workers_keep_database(d, plain)


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    plain = dump(args.binary, None, headless=not args.headful, extra_args=host_gpu_args(not args.headful))
    if plain["context_errors"]:
        raise GuardFailure(f"precondition contexts failed: {plain['context_errors']}")
    check_no_decode(args, profile, plain)
    check_claim(args, profile, plain, f"{profile['os']['family']}-claims-decode")
    if profile["os"]["family"] != "windows":
        check_claim(args, windows_variant(profile), plain, "windows-claims-decode")


finish(main)
