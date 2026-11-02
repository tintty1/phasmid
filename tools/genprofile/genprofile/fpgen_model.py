"""The pinned fpgen model (Scrapfly's fingerprint-generator), read directly.

fpgen itself is never imported: importing it downloads a model with TLS
verification off and replaces it every few weeks. ``fetch`` downloads the
release named in ``fpgen-model.json`` with TLS verification on, checks the
archive, every compressed file and every decompressed file against the pin,
and installs them under ``$XDG_CACHE_HOME/phasmid/fpgen/<archive sha256>/``.
Any mismatch is refused. The model data carries no licence, so it is never
copied into the repository; only the extract under profiles/data/fpgen/ is.

The model is a Bayesian network whose tables are unsmoothed session
frequencies: a probability times the session count of its parent
configuration is an integer, which ``support`` recovers and checks. A value id
is a base85 integer indexing the ordered items of values.json, each a
(hex offset, length) slice of values.dat holding one JSON value.

    python -m genprofile.fpgen_model [--archive model-release.zip]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import mmap
import os
import shutil
import ssl
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import zstandard

PIN_PATH = Path(__file__).resolve().parents[1] / "fpgen-model.json"
_INTEGRAL = 1e-6


class ModelError(Exception):
    pass


@dataclass(frozen=True)
class Pin:
    tag: str
    url: str
    size: int
    sha256: str
    files: dict[str, tuple[str, str]]  # name -> (sha256 of name.zst, sha256 of name)


@cache
def pin() -> Pin:
    raw = json.loads(PIN_PATH.read_text())
    files = {name: (f["zst_sha256"], f["sha256"]) for name, f in raw["files"].items()}
    return Pin(raw["tag"], raw["url"], raw["size"], raw["sha256"], files)


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "phasmid" / "fpgen"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(directory: Path, p: Pin) -> None:
    for name, (_, sha) in p.files.items():
        path = directory / name
        if not path.is_file():
            raise ModelError(f"{path}: missing")
        if _file_sha256(path) != sha:
            raise ModelError(f"{path}: sha256 differs from the pin")


def install(archive: Path, p: Pin, root: Path) -> Path:
    """Unpack a downloaded release archive into ``root/<sha256>/`` after checking every hash."""
    data = archive.read_bytes()
    if len(data) != p.size or hashlib.sha256(data).hexdigest() != p.sha256:
        raise ModelError(f"{archive}: size or sha256 differs from the pin ({p.tag})")
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=root, prefix=".partial-"))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = sorted(z.namelist())
            if members != sorted(f"{name}.zst" for name in p.files):
                raise ModelError(f"{archive}: members {members} differ from the pin")
            for name, (zst_sha, _) in p.files.items():
                compressed = z.read(f"{name}.zst")
                if hashlib.sha256(compressed).hexdigest() != zst_sha:
                    raise ModelError(f"{archive}: {name}.zst sha256 differs from the pin")
                with (tmp / name).open("wb") as out:
                    zstandard.ZstdDecompressor().copy_stream(io.BytesIO(compressed), out)
        verify(tmp, p)
        dest = root / p.sha256
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def _download(url: str, dest: Path) -> None:
    context = ssl.create_default_context()  # verifies the certificate chain and host name
    with urllib.request.urlopen(url, context=context, timeout=120) as response:
        if not response.geturl().startswith("https://"):
            raise ModelError(f"{url}: redirected off https to {response.geturl()}")
        with dest.open("wb") as out:
            shutil.copyfileobj(response, out)


def fetch(p: Pin | None = None, root: Path | None = None, archive: Path | None = None) -> Path:
    """The verified model directory, downloading the pinned release (or installing
    ``archive``, a copy of it) when the cache does not have it yet."""
    p = p or pin()
    root = root or cache_root()
    dest = root / p.sha256
    if dest.is_dir():
        verify(dest, p)
        return dest
    if archive is not None:
        return install(archive, p, root)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        download = Path(tmp) / "model-release.zip"
        _download(p.url, download)
        return install(download, p, root)


def support(count: float) -> int:
    """A session count recovered as n * P, which must be a whole number."""
    rounded = round(count)
    if abs(count - rounded) > _INTEGRAL:
        raise ModelError(f"session count {count} is not integral: the tables are not raw frequencies")
    return rounded


class Model:
    def __init__(self, directory: Path):
        network = json.loads((directory / "fingerprint-network.json").read_text())
        self.nodes: dict[str, dict[str, Any]] = {n["name"]: n for n in network["nodes"]}
        self._index = list(json.loads((directory / "values.json").read_text()).items())
        with (directory / "values.dat").open("rb") as f:
            self._dat = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        self._values: dict[str, Any] = {}

    def val(self, vid: str) -> Any:
        if vid not in self._values:
            offset, length = self._index[int.from_bytes(base64.b85decode(vid), "big")]
            start = int(offset, 16)
            self._values[vid] = json.loads(self._dat[start:start + length].decode())
        return self._values[vid]

    def dist(self, name: str, known: dict[str, str]) -> dict[str, float]:
        """P(name | its parents), the parents' value ids taken from ``known``."""
        node = self.nodes[name]
        table = node["conditionalProbabilities"]
        for parent in node["parentNames"]:
            table = table.get(known[parent], {})
        return table

    def sessions(self, name: str, known: dict[str, str], limit: int = 100_000) -> int:
        """The session count behind one table: the smallest n that makes every entry whole.
        Subtrees differ (sessions missing a value are left out of that node's table)."""
        probs = list(self.dist(name, known).values())
        for n in range(1, limit):
            if all(abs(p * n - round(p * n)) <= _INTEGRAL for p in probs):
                return n
        raise ModelError(f"{name}: no session count below {limit} makes the table whole")

    def enum(self, names: list[str], known: dict[str, str], n: float) -> Iterator[tuple[dict[str, str], float]]:
        """Every assignment of the chain ``names`` given ``known``, with its count n * P."""
        def walk(i: int, assigned: dict[str, str], count: float) -> Iterator[tuple[dict[str, str], float]]:
            if i == len(names):
                yield dict(assigned), count
                return
            for vid, p in self.dist(names[i], {**known, **assigned}).items():
                if p > 0:
                    assigned[names[i]] = vid
                    yield from walk(i + 1, assigned, count * p)
                    del assigned[names[i]]
        yield from walk(0, {}, n)


@cache
def model() -> Model:
    return Model(fetch())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="genprofile.fpgen_model", description="Download and verify the pinned fpgen model.")
    ap.add_argument("--archive", type=Path, help="install this copy of the release archive instead of downloading it")
    args = ap.parse_args(argv)
    try:
        print(fetch(archive=args.archive))
    except ModelError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
