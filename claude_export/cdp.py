"""Minimal Chrome DevTools Protocol client.

Only what this tool needs: attach to the browser, open scratch tabs, evaluate
JS in a page, and capture rendered output (MHTML / PDF / DOM).
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request

try:
    import websocket
except ImportError as e:  # pragma: no cover
    raise SystemExit("missing dependency: run `pip install -r requirements.txt`") from e


# A machine-wide http_proxy would otherwise swallow 127.0.0.1 and the devtools
# endpoint would "never open" for no apparent reason.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# Raised by the protocol when the page's JS context went away mid-run (the SPA
# navigated, a service worker reloaded, or the renderer crashed). Every later
# evaluate on that session fails until a fresh tab is opened.
_CONTEXT_LOST = (
    "Cannot find context",
    "Execution context was destroyed",
    "Session with given id not found",
    "No target with given id",
)


def is_context_lost(err: Exception) -> bool:
    text = str(err)
    return any(marker in text for marker in _CONTEXT_LOST)


def browser_ws(port: int, timeout: float = 40.0) -> str:
    """Return the browser-level webSocketDebuggerUrl, waiting for the port to open."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with _LOCAL.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return json.load(r)["webSocketDebuggerUrl"]
        except Exception as e:  # noqa: BLE001 - port may not be listening yet
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"devtools port {port} never opened: {last}")


class CDP:
    """A flattened CDP session against the browser endpoint."""

    def __init__(self, ws_url: str, timeout: int = 180):
        self.ws = websocket.create_connection(ws_url, max_size=None, timeout=timeout)
        self._id = 0

    # -- plumbing ---------------------------------------------------------
    def send(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        while True:
            data = json.loads(self.ws.recv())
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    # -- pages ------------------------------------------------------------
    def new_page(self, url: str = "about:blank") -> tuple[str, str]:
        """Create a tab. Returns (target_id, session_id)."""
        target = self.send("Target.createTarget", {"url": url})
        tid = target["targetId"]
        sess = self.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        self.send("Page.enable", session=sess)
        self.send("Runtime.enable", session=sess)
        return tid, sess

    def close_page(self, target_id: str) -> None:
        try:
            self.send("Target.closeTarget", {"targetId": target_id})
        except Exception:  # noqa: BLE001
            pass

    def navigate(self, session: str, url: str, settle: float = 0.0) -> None:
        self.send("Page.navigate", {"url": url}, session=session)
        if settle:
            time.sleep(settle)

    def eval(self, session: str, expr: str, await_promise: bool = False):
        res = self.send(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": await_promise, "returnByValue": True},
            session=session,
        )
        if "exceptionDetails" in res:
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:800])
        return res["result"].get("value")

    def fetch_json(self, session: str, path: str, headers: dict | None = None) -> tuple[int, object]:
        """GET a same-origin path from inside the page; returns (status, parsed)."""
        hdr = json.dumps(headers or {"accept": "application/json"})
        expr = (
            "(async()=>{const r=await fetch(%s,{headers:%s,credentials:'include'});"
            "const t=await r.text();return JSON.stringify({s:r.status,b:t})})()"
        ) % (json.dumps(path), hdr)
        out = json.loads(self.eval(session, expr, await_promise=True))
        try:
            return out["s"], json.loads(out["b"])
        except Exception:  # noqa: BLE001 - body may not be JSON
            return out["s"], out["b"]

    # -- capture ----------------------------------------------------------
    def print_pdf(self, session: str, **kwargs) -> bytes:
        params = {"printBackground": True}
        params.update(kwargs)
        return base64.b64decode(self.send("Page.printToPDF", params, session=session)["data"])

    def capture_mhtml(self, session: str) -> str:
        return self.send("Page.captureSnapshot", {"format": "mhtml"}, session=session).get("data", "")

    def outer_html(self, session: str) -> str:
        return self.eval(session, "document.documentElement.outerHTML") or ""

    # -- downloads --------------------------------------------------------
    def read_buffer(self, session: str, total: int, chunk: int = 2_000_000) -> bytes:
        """Base64 the page's `window.__buf` a chunk at a time and reassemble it."""
        parts: list[str] = []
        for start in range(0, total, chunk):
            end = min(start + chunk, total)
            parts.append(self.eval(session, f"""(()=>{{
              const sl = window.__buf.subarray({start},{end});
              let s=''; const C=0x8000;
              for(let k=0;k<sl.length;k+=C) s += String.fromCharCode.apply(null, sl.subarray(k,k+C));
              return btoa(s);
            }})()""") or "")
        # Decode per chunk: each one is independently padded, so concatenating the
        # base64 text first would corrupt anything past the first chunk boundary.
        return b"".join(base64.b64decode(part) for part in parts if part)

    def fetch_bytes(self, session: str, url: str, chunk: int = 2_000_000) -> bytes:
        """Download a URL through the page (inherits cookies/headers).

        The body is kept as a Uint8Array in the page and base64-encoded a chunk at
        a time, so memory stays bounded instead of materialising one string for
        the whole file (measured: an 80MB body would need 111MB of base64).
        """
        prep = f"""(async()=>{{
          const r = await fetch({json.dumps(url)}, {{credentials:'include'}});
          if(!r.ok) return 'HTTP'+r.status;
          window.__buf = new Uint8Array(await r.arrayBuffer());
          return 'OK:'+window.__buf.length;
        }})()"""
        out = self.eval(session, prep, await_promise=True) or ""
        if not out.startswith("OK:"):
            raise RuntimeError(out or "empty response")
        total = int(out[3:])
        try:
            return self.read_buffer(session, total, chunk)
        finally:
            self.eval(session, "window.__buf = null")
