"""Chrome DevTools Protocol harness for the Task 4.7 acceptance checks (standard library only).

It launches a real Chrome or Edge (headless, an isolated temporary profile outside the repository), talks to
it over the DevTools WebSocket with a small RFC 6455 client, and drives a real page: navigation, JavaScript
evaluation, real key events (Input.dispatchKeyEvent), viewport emulation, screenshots, and capture of
console messages, uncaught exceptions, Content-Security-Policy violations and failed or error HTTP requests.

It never touches a browser that it did not start. The Viewer under test is launched as a subprocess with the
real command line (`python -m coroner.viewer`), and only that subprocess is stopped afterwards.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tests.unit.viewer.browser_harness import find_browser

REPO = Path(__file__).resolve().parents[3]


class CdpError(RuntimeError):
    pass


# ------------------------------------------------------------------ WebSocket client


class WebSocket:
    """A minimal client: text frames only, masked on send, ping answered, fragments joined."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 30.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("DevTools WebSocket closed during the handshake")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CdpError("DevTools WebSocket handshake refused: %r" % head[:200])
        self.buf = rest

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CdpError("DevTools WebSocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self, timeout: float) -> str | None:
        """The next text message, or None when nothing arrives within `timeout` seconds."""
        self.sock.settimeout(timeout)
        message = b""
        try:
            while True:
                b0, b1 = self._read(2)
                opcode = b0 & 0x0F
                length = b1 & 0x7F
                if length == 126:
                    (length,) = struct.unpack(">H", self._read(2))
                elif length == 127:
                    (length,) = struct.unpack(">Q", self._read(8))
                payload = self._read(length)
                if opcode == 9:
                    self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                    continue
                if opcode == 8:
                    raise CdpError("DevTools WebSocket closed by the browser")
                message += payload
                if b0 & 0x80:
                    return message.decode("utf-8")
        except socket.timeout:
            return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ the page


CAPTURE_INIT_JS = r"""
(() => {
  window.__csp = [];
  document.addEventListener("securitypolicyviolation", (e) => {
    window.__csp.push(e.violatedDirective + " | " + e.blockedURI + " | " + e.effectiveDirective);
  });
})();
"""

KEYS = {
    "Tab": ("Tab", "Tab", 9, ""),
    "Enter": ("Enter", "Enter", 13, "\r"),
    " ": (" ", "Space", 32, " "),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, ""),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "Home": ("Home", "Home", 36, ""),
    "End": ("End", "End", 35, ""),
}


class Page:
    """One real browser page. Events are collected per navigation in `self.capture`."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._id = 0
        self.pending_events: list[dict[str, Any]] = []
        self.capture = self._fresh_capture()
        self.request_urls: dict[str, str] = {}

    @staticmethod
    def _fresh_capture() -> dict[str, list[Any]]:
        return {
            "console_errors": [], "console_warnings": [], "exceptions": [], "log_errors": [], "log_warnings": [],
            "failed_requests": [], "http_errors": [], "responses": [],
        }

    # -- protocol -----------------------------------------------------
    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise CdpError(f"timeout waiting for {method}")
            raw = self.ws.recv(min(left, 1.0))
            if raw is None:
                continue
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._on_event(msg)

    def pump(self, seconds: float) -> None:
        """Read events for up to `seconds` (used while waiting for a condition, never as a fixed sleep)."""
        raw = self.ws.recv(seconds)
        if raw is not None:
            self._on_event(json.loads(raw))

    def _on_event(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        p = msg.get("params", {})
        c = self.capture
        if method == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
            if p.get("type") == "error":
                c["console_errors"].append(text)
            elif p.get("type") == "warning":
                c["console_warnings"].append(text)
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            c["exceptions"].append((d.get("exception") or {}).get("description") or d.get("text", "exception"))
        elif method == "Log.entryAdded":
            e = p.get("entry", {})
            item = f"{e.get('source')}: {e.get('text')} {e.get('url', '')}".strip()
            if e.get("level") == "error":
                c["log_errors"].append(item)
            elif e.get("level") == "warning":
                c["log_warnings"].append(item)
        elif method == "Network.requestWillBeSent":
            self.request_urls[p["requestId"]] = p["request"]["url"]
        elif method == "Network.responseReceived":
            r = p["response"]
            c["responses"].append({"url": r["url"], "status": r["status"], "mime": r.get("mimeType")})
            if r["status"] >= 400:
                c["http_errors"].append({"url": r["url"], "status": r["status"]})
        elif method == "Network.loadingFailed":
            c["failed_requests"].append(
                {"url": self.request_urls.get(p["requestId"], "?"), "error": p.get("errorText"), "canceled": p.get("canceled")}
            )
        self.pending_events.append(msg)

    # -- evaluation ---------------------------------------------------
    def eval(self, expression: str, await_promise: bool = False) -> Any:
        r = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise, "userGesture": True},
        )
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise CdpError("page script error: " + str((d.get("exception") or {}).get("description") or d.get("text")))
        return r["result"].get("value")

    def wait_for(self, expression: str, timeout: float = 20.0, what: str = "condition") -> None:
        """Poll a JS boolean expression until true (no fixed sleep); raises on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval(expression):
                return
            self.pump(0.05)
        raise CdpError(f"timeout waiting for {what}: {expression[:120]}")

    # -- navigation ---------------------------------------------------
    def navigate(self, url: str, ready: str | None = None, timeout: float = 20.0) -> dict[str, Any]:
        """Clear the capture, navigate, wait for load and for `ready` (a JS boolean). Returns the main response."""
        self.capture = self._fresh_capture()
        self.request_urls = {}
        result = self.call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise CdpError("navigation failed: " + result["errorText"])
        self.wait_for("document.readyState === 'complete'", timeout, "document load")
        if ready:
            self.wait_for(ready, timeout, "page render")
        main = next((r for r in self.capture["responses"] if r["url"] == url or r["url"].split("#")[0] == url), None)
        return main or {}

    def csp_events(self) -> list[str]:
        return self.eval("window.__csp || []") or []

    def report(self) -> dict[str, Any]:
        """Everything captured since the last navigation, as plain data."""
        c = self.capture
        return {
            "console_errors": list(c["console_errors"]),
            "console_warnings": list(c["console_warnings"]),
            "uncaught_exceptions": list(c["exceptions"]),
            "log_errors": list(c["log_errors"]),
            "log_warnings": list(c["log_warnings"]),
            "csp_violations": self.csp_events(),
            "failed_requests": list(c["failed_requests"]),
            "http_errors": list(c["http_errors"]),
            "fallback_page_shown": bool(self.eval("document.body.innerText.includes('The page could not be displayed')")),
        }

    # -- input --------------------------------------------------------
    def key(self, name: str, shift: bool = False) -> None:
        key, code, vk, text = KEYS[name]
        mods = 8 if shift else 0
        down = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mods}
        if text:
            down["type"] = "keyDown"
            down["text"] = text
        self.call("Input.dispatchKeyEvent", down)
        self.call("Input.dispatchKeyEvent", {**down, "type": "keyUp", "text": ""})

    def click(self, selector: str) -> None:
        """A real mouse click at the centre of the first element matching `selector`."""
        box = self.eval(
            "(() => { const e = document.querySelector(%s); if (!e) return null; e.scrollIntoView({block:'center'});"
            " const r = e.getBoundingClientRect(); return [r.left + r.width/2, r.top + r.height/2]; })()" % json.dumps(selector)
        )
        if not box:
            raise CdpError("click target not found: " + selector)
        x, y = box
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1})

    # -- viewport and screenshots ------------------------------------
    def set_viewport(self, width: int, height: int = 900) -> None:
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )

    def screenshot(self, path: Path, full_page: bool = True) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        params: dict[str, Any] = {"format": "png"}
        if full_page:
            m = self.call("Page.getLayoutMetrics")
            size = m.get("cssContentSize") or m["contentSize"]
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": 0, "y": 0, "width": size["width"], "height": min(size["height"], 6000), "scale": 1}
        data = self.call("Page.captureScreenshot", params)["data"]
        path.write_bytes(base64.b64decode(data))
        return {"path": str(path), "bytes": path.stat().st_size}


    def screenshot_element(self, path: Path, selector: str, pad: int = 12) -> dict[str, Any]:
        """Screenshot of one element (and a margin) in page coordinates, from a real render."""
        box = self.eval(
            "(() => { const e = document.querySelector(%s); if (!e) return null; const r = e.getBoundingClientRect();"
            " return [r.left + scrollX, r.top + scrollY, r.width, r.height]; })()" % json.dumps(selector)
        )
        if not box:
            raise CdpError("screenshot target not found: " + selector)
        path.parent.mkdir(parents=True, exist_ok=True)
        x, y, w, h = box
        clip = {"x": max(0, x - pad), "y": max(0, y - pad), "width": w + 2 * pad, "height": h + 2 * pad, "scale": 1}
        data = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": clip})["data"]
        path.write_bytes(base64.b64decode(data))
        return {"path": str(path), "bytes": path.stat().st_size}


# ------------------------------------------------------------------ browser and viewer processes


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop_tree(proc: subprocess.Popen[bytes]) -> None:
    """Stop a process this harness started, with its children (the Windows venv launcher starts a child Python)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


class Chrome:
    """A real Chrome started by this harness with an isolated profile outside the repository."""

    def __init__(self, browser: str | None = None, headless: bool = True) -> None:
        exe = browser or find_browser()
        if exe is None:
            raise CdpError("no Chrome, Chromium or Edge found")
        self.exe = exe
        self.profile = tempfile.mkdtemp(prefix="coroner-47-profile-")
        self.port = _free_port()
        args = [
            exe, f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
            "--disable-gpu", "--hide-scrollbars", "--window-size=1280,900", "about:blank",
        ]
        if headless:
            args.insert(1, "--headless=new")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.version = ""
        self.page = self._attach()

    def _json(self, path: str) -> Any:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def _attach(self) -> Page:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                self.version = self._json("/json/version")["Browser"]
                targets = [t for t in self._json("/json/list") if t.get("type") == "page"]
                if targets:
                    path = targets[0]["webSocketDebuggerUrl"].split(str(self.port), 1)[1]
                    page = Page(WebSocket("127.0.0.1", self.port, path))
                    for method in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
                        page.call(method)
                    page.call("Page.addScriptToEvaluateOnNewDocument", {"source": CAPTURE_INIT_JS})
                    page.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
                    return page
            except (OSError, urllib.error.URLError, KeyError, ValueError):
                pass
            time.sleep(0.2)
        self.close()
        raise CdpError("Chrome did not expose a DevTools page in time")

    def close(self) -> None:
        page = getattr(self, "page", None)
        if page is not None:
            page.ws.close()
        _stop_tree(self.proc)
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self) -> "Chrome":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class ViewerProcess:
    """The Viewer started with the real command line `python -m coroner.viewer` on a free port."""

    def __init__(self, runs_dir: str | Path, python: str | None = None, port: int | None = None) -> None:
        self.port = port or _free_port()
        self.runs_dir = str(runs_dir)
        self.command = [python or sys.executable, "-m", "coroner.viewer", "--runs-dir", self.runs_dir,
                        "--host", "127.0.0.1", "--port", str(self.port)]
        self.log = tempfile.NamedTemporaryFile(prefix="coroner-47-viewer-", suffix=".log", delete=False)
        self.proc = subprocess.Popen(self.command, cwd=str(REPO), stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise CdpError("the Viewer exited at start; see " + self.log.name)
            try:
                with urllib.request.urlopen(self.base + "/api/runs", timeout=2):
                    return
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        self.close()
        raise CdpError("the Viewer did not answer in time")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        _stop_tree(self.proc)
        self.log.close()

    def __enter__(self) -> "ViewerProcess":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def get(url: str) -> tuple[int, dict[str, str], bytes]:
    """A plain HTTP GET (status, lower-cased headers, body); error statuses are returned, not raised."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()
