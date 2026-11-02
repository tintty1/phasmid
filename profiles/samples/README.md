# Generated sample profiles

These profiles are **generated, not captured**. They exist to develop and test patches against more than one device shape; they are not examples of a real machine (those are in `../examples/`, each with its dump under `../data/captures/`).

Each one is a BrowserForge sample (`browserforge` and `apify_fingerprint_datapoints`, pinned in `tools/genprofile/pyproject.toml`) for the OS identity, `hardware.concurrency`, `device_memory`, `max_touch_points`, screen and window geometry, media device presence and battery, combined with the device data of the one Linux capture (`linux-arch-rtx3060-1080p`): GPU, fonts, voices, audio, codecs, system colours, pointer and preferences, and the labels of its first media devices of each kind the sample has (the capture record lists them, from the host after permission). The GPU's identity is either the capture's RTX 3060 or the RTX 3070 Ti, the one GPU its host class may claim (`profiles/data/fpgen/linux.json`: 2 clean fpgen sessions report exactly the capture's WebGL surface), weighted 1 (the capture) to 2 (those sessions); only the identity strings and the device id are derived, every limit, extension and WebGPU value is the capture's. `captured.note` records the package versions, the hash of the fingerprint network and, for a derived identity, the fpgen model, value ids and device-id sources; `captured.dump_sha256` names the capture.

| Profile | Seed | Languages, timezone | Shape | GPU |
| --- | --- | --- | --- | --- |
| `gen-linux-1920x1080-a493e899` | `a493e899298cf428204ebb2a4200743c` | `en-US,en`, America/New_York | 8 threads, 32 GB, 1920x1080 at DPR 1, window 1920x1048 | RTX 3070 Ti (0x2482) |
| `gen-linux-1707x960-be27e761` | `be27e761da79e300f463b777cfc90d01` | `de-DE,de,en-US,en`, Europe/Berlin | 16 threads, 32 GB, 1707x960 at DPR 1.5, window 1707x916, no microphone | RTX 3070 Ti (0x2482) |
| `gen-linux-3072x1728-6e34c862` | `6e34c862456a8112b725d8fbcebc98a0` | `en-US,en`, America/Los_Angeles | 20 threads, 8 GB, 3072x1728 at DPR 1.25, window 2452x1549 | RTX 3060 (0x2504), the capture's |

Known limits: concurrency, memory and screen are independent draws (BrowserForge's network has no edges between them), so the hardware group is not ruled against the GPU; a profile whose memory bucket differs from the capture's omits `physical_memory_mb` and `js_heap_size_limit`. A derived RTX 3070 Ti keeps the capture's driver suffix (615.71.09); the driver table it was checked against is the host's 615.78.08 README, the same 615 branch.

Regenerate (byte for byte; `tools/genprofile/test_genprofile.py` checks it):

```bash
cd tools/genprofile
uv run python -m genprofile --os linux --seed a493e899298cf428204ebb2a4200743c --timezone America/New_York --languages en-US,en -o ../../profiles/samples/gen-linux-1920x1080-a493e899.json
uv run python -m genprofile --os linux --seed be27e761da79e300f463b777cfc90d01 --timezone Europe/Berlin --languages de-DE,de,en-US,en -o ../../profiles/samples/gen-linux-1707x960-be27e761.json
uv run python -m genprofile --os linux --seed 6e34c862456a8112b725d8fbcebc98a0 --timezone America/Los_Angeles --languages en-US,en -o ../../profiles/samples/gen-linux-3072x1728-6e34c862.json
```

Windows profiles take their device data from the Windows capture (`win11-uhd600-1440x960`); an OS no capture covers stops the generator, naming the fields it cannot fill. Generate Windows profiles with `--exit-ip` (`tools/genprofile/README.md`): Windows Chrome serves a position from the system location provider, so a Windows profile without `locale.geolocation` is a stronger tell than a Linux one.
