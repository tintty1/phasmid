"""Launch a Chromium binary the way a user would, then attach over CDP.

Playwright's own ``chromium.launch`` adds a long list of switches
(``--enable-automation``, ``--remote-debugging-pipe``, feature overrides,
``--hide-scrollbars`` in headless) that change several surfaces this harness
measures, so the binary is started with only a fixed debugging port. A
non-zero ``--remote-debugging-port`` does not turn on ``AutomationControlled``
(``content/child/runtime_features.cc``); port 0, ``--remote-debugging-pipe``
and ``--enable-automation`` do.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Browser, Playwright

# Playwright emulates prefers-color-scheme, prefers-reduced-motion,
# forced-colors and prefers-contrast on every page it attaches to, defaulting
# to light / no-preference / none / no-preference when the context has no
# options, which a connect_over_cdp default context never has. Passed to
# page.emulate_media, "null" clears each override so the browser's own values
# reach the page and the Sec-CH-Prefers-* headers.
NO_MEDIA_EMULATION = {"color_scheme": "null", "reduced_motion": "null", "forced_colors": "null", "contrast": "null"}

DEFAULT_BINARIES = [
    Path(__file__).resolve().parents[3] / "dist",  # ../dist/<build>/chrome, searched below
    Path("/usr/bin/google-chrome-stable"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium"),
]


def find_default_binary() -> Path | None:
    for candidate in DEFAULT_BINARIES:
        if candidate.is_dir():
            for chrome in sorted(candidate.glob("*/chrome")):
                if os.access(chrome, os.X_OK):
                    return chrome
        elif candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def binary_version(binary: Path) -> str:
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or out.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"


@dataclass
class Launch:
    binary: Path
    headless: bool = True
    profile: Path | None = None
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout: float = 60.0

    # Set by start()
    port: int = 0
    user_data_dir: Path | None = None
    process: subprocess.Popen | None = None
    browser: Browser | None = None
    command: list[str] = field(default_factory=list)
    stderr_path: Path | None = None
    devtools_version: dict = field(default_factory=dict)
    _stderr_file: object = None

    def start(self, playwright: Playwright) -> Browser:
        self.port = _free_port()
        self.user_data_dir = Path(tempfile.mkdtemp(prefix="phasmid-ud-"))
        args = [
            str(self.binary),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.headless:
            args.append("--headless=new")
        if self.profile is not None:
            args.append(f"--phasmid-profile={Path(self.profile).resolve()}")
        args += self.extra_args
        args.append("about:blank")
        self.command = args

        env = dict(os.environ)
        env.update(self.env)
        # stderr goes to a file: Chrome logs enough that an undrained pipe
        # fills and blocks the browser before DevToolsActivePort is written.
        self.stderr_path = self.user_data_dir / "phasmid-stderr.log"
        self._stderr_file = open(self.stderr_path, "w")
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=self._stderr_file, env=env, text=True
        )

        # Poll the DevTools HTTP endpoint. Chrome does not reliably write
        # DevToolsActivePort in --headless=new, so the file is not a signal.
        deadline = time.monotonic() + self.startup_timeout
        endpoint = f"http://127.0.0.1:{self.port}/json/version"
        while True:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"browser exited with {self.process.returncode} before DevTools came up\n{self.stderr_tail()}"
                )
            try:
                with urllib.request.urlopen(endpoint, timeout=1) as resp:
                    self.devtools_version = json.loads(resp.read().decode())
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
                pass
            if time.monotonic() > deadline:
                self.stop()
                raise TimeoutError(f"DevTools endpoint {endpoint} never answered")
            time.sleep(0.05)

        self.browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
        return self.browser

    def stderr_tail(self, limit: int = 4000) -> str:
        if self.stderr_path is None:
            return ""
        try:
            self._stderr_file.flush()
            return self.stderr_path.read_text(errors="replace")[-limit:]
        except (OSError, ValueError):
            return ""

    def stop(self) -> None:
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:  # noqa: BLE001 (best-effort teardown)
                pass
            self.browser = None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            except OSError:
                pass
            self._stderr_file = None
        if self.user_data_dir is not None:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
            self.user_data_dir = None
