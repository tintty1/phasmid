"""The pinned DB-IP IP to City Lite database (CC BY 4.0, https://db-ip.com).

The database is about 127 MB, so it is never committed. ``reader`` downloads
the build named in ``dbip-city-lite.json`` with TLS verification on, checks the
archive and the decompressed file against the pin, and installs it under
``$XDG_CACHE_HOME/phasmid/geoip/<archive sha256>/``. Any mismatch is refused.
DB-IP serves only the current and the previous month's build; once the pinned
one is gone, install a copy of the same file with ``--archive`` or re-pin.

    python -m genprofile.geoip [--archive dbip-city-lite-YYYY-MM.mmdb.gz]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import ssl
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import maxminddb

PIN_PATH = Path(__file__).resolve().parents[1] / "dbip-city-lite.json"
_MMDB = "dbip-city-lite.mmdb"


class GeoipError(Exception):
    pass


@dataclass(frozen=True)
class Pin:
    name: str
    build: str
    url: str
    size: int
    sha256: str
    mmdb_size: int
    mmdb_sha256: str
    licence: str
    attribution: str


@cache
def pin() -> Pin:
    return Pin(**json.loads(PIN_PATH.read_text()))


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "phasmid" / "geoip"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(path: Path, p: Pin) -> None:
    if not path.is_file():
        raise GeoipError(f"{path}: missing")
    if path.stat().st_size != p.mmdb_size or _file_sha256(path) != p.mmdb_sha256:
        raise GeoipError(f"{path}: size or sha256 differs from the pin ({p.name} {p.build})")


def install(archive: Path, p: Pin, root: Path) -> Path:
    """Decompress a downloaded archive into ``root/<sha256>/`` after checking both hashes."""
    if archive.stat().st_size != p.size or _file_sha256(archive) != p.sha256:
        raise GeoipError(f"{archive}: size or sha256 differs from the pin ({p.name} {p.build}, {p.url})")
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=root, prefix=".partial-"))
    try:
        with gzip.open(archive, "rb") as src, (tmp / _MMDB).open("wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        verify(tmp / _MMDB, p)
        dest = root / p.sha256
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest / _MMDB


def _download(url: str, dest: Path) -> None:
    context = ssl.create_default_context()  # verifies the certificate chain and host name
    # DB-IP's CDN answers Python's default User-Agent with 403.
    request = urllib.request.Request(url, headers={"User-Agent": "phasmid-genprofile"})
    try:
        with urllib.request.urlopen(request, context=context, timeout=120) as response, dest.open("wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
    except OSError as e:
        raise GeoipError(
            f"cannot download {url} ({e}); DB-IP keeps only the last two monthly builds. Install a copy "
            "of the pinned file with python -m genprofile.geoip --archive <file>, or re-pin dbip-city-lite.json"
        ) from e


def fetch(archive: Path | None = None) -> Path:
    """The verified mmdb, downloading and installing the pinned build when it is not cached."""
    p = pin()
    path = cache_root() / p.sha256 / _MMDB
    if archive is None and path.is_file():
        verify(path, p)
        return path
    if archive is not None:
        return install(archive, p, cache_root())
    cache_root().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_root(), prefix=".download-") as tmp:
        downloaded = Path(tmp) / "archive.mmdb.gz"
        _download(p.url, downloaded)
        return install(downloaded, p, cache_root())


@cache
def reader() -> maxminddb.Reader:
    path = fetch()
    db = maxminddb.open_database(str(path))
    if db.metadata().database_type != "DBIP-City-Lite":
        raise GeoipError(f"{path}: database type {db.metadata().database_type}, expected DBIP-City-Lite")
    return db


def provenance() -> str:
    p = pin()
    return f"{p.name} {p.build} (mmdb sha256 {p.mmdb_sha256}, {p.attribution}, {p.licence.split(',')[0]})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="genprofile.geoip", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", type=Path, help="a local copy of the pinned .mmdb.gz instead of downloading it")
    args = ap.parse_args(argv)
    try:
        print(fetch(args.archive))
    except GeoipError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
