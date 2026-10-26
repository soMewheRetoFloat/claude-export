"""Get an authenticated claude.ai browser session on macOS.

Why this is not just "copy the cookie":
  claude.ai accepts a valid `sessionKey` only when the surrounding profile state
  (localStorage / IndexedDB / device ids) is present too. Injecting cookies into a
  fresh profile gets a 403 -> /login even though the cookie itself is fine.
  Copying the real profile and launching Edge against the copy works.

Edge also refuses --remote-debugging-port on the *default* profile directory
(Chromium 136+), which is the other reason a copy is used.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .cdp import CDP, browser_ws

EDGE_APP = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PROFILE_DIR = Path.home() / "Library/Application Support/Microsoft Edge"

# Profile sub-trees that are large and useless for authentication.
PROFILE_EXCLUDES = [
    "Default/Extensions/",
    "Default/load_statistics.db",
    "Default/Service Worker/CacheStorage/",
    "Default/Code Cache/",
    "Default/GPUCache/",
    "Default/Cache/",
    "Default/DawnGraphiteCache/",
    "Default/DawnWebGPUCache/",
    "Default/Shared Dictionary/",
    "Crashpad/",
    "BrowserMetrics",
]


# ---------------------------------------------------------------- cookie db
def cookie_names(profile: Path, host_suffix: str = "claude.ai") -> list[str]:
    """Cookie *names* stored for a host. Metadata only - nothing is decrypted.

    Decrypting values would require the Keychain item "Microsoft Edge Safe Storage",
    which this tool deliberately never touches: launching Edge against a copy of the
    profile lets the browser decrypt its own cookies, so we never need the key.
    """
    import sqlite3
    import tempfile

    db = profile / "Default/Cookies"
    if not db.exists():
        raise FileNotFoundError(db)

    tmp = Path(tempfile.mkdtemp()) / "cookies.db"
    shutil.copy2(db, tmp)
    con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
    try:
        rows = con.execute(
            "select distinct name from cookies where host_key like ?", (f"%{host_suffix}%",)
        )
        return sorted(r[0] for r in rows)
    finally:
        con.close()
        shutil.rmtree(tmp.parent, ignore_errors=True)


# ---------------------------------------------------------------- profile copy
def copy_profile(dest: Path, source: Path | None = None, quiet: bool = False) -> Path:
    source = (source or PROFILE_DIR).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Edge profile not found: {source}")
    dest = dest.expanduser()

    # --profile-copy pointing at (or inside) the live profile would make the
    # rmtree below destroy the user's real browser data. Refuse outright.
    src_r, dst_r = source.resolve(), dest.resolve()
    if dst_r == src_r or src_r in dst_r.parents or dst_r in src_r.parents:
        raise ValueError(
            f"refusing to use {dest} as the profile copy: it overlaps the real "
            f"Edge profile at {source}. Pick a path outside it, e.g. /tmp/edge-profile."
        )

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    args = ["rsync", "-a"]
    for pattern in PROFILE_EXCLUDES:
        args += ["--exclude", pattern]
    args += [f"{source}/", f"{dest}/"]
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    # 23/24 = "some files vanished / partial transfer". That is normal when Edge is
    # running and rotating its LevelDB logs; the auth state we need is already there.
    if proc.returncode in (23, 24) and (dest / "Default/Cookies").exists():
        if not quiet:
            print("note: profile changed while copying (Edge is running); continuing",
                  file=sys.stderr)
    elif proc.returncode != 0:
        raise RuntimeError(
            "copying the Edge profile failed (rsync exit "
            f"{proc.returncode}): {(proc.stderr or '').strip()[:300]}"
        )

    # SingletonLock/Socket/Cookie are left behind by the running browser and point
    # at *its* IPC endpoint. Keeping them makes the copied profile hand off to the
    # live Edge instead of starting a new instance ("opening in existing session").
    for singleton in ("SingletonLock", "SingletonSocket", "SingletonCookie",
                      "Default/SingletonLock", "Default/SingletonSocket",
                      "Default/SingletonCookie"):
        (dest / singleton).unlink(missing_ok=True)

    # Do not resurrect the user's open tabs: restoring them would make this tool
    # fetch unrelated sites with their session, which is both noisy and leaky.
    for leftover in ("Default/Current Session", "Default/Last Session",
                     "Default/Current Tabs", "Default/Last Tabs",
                     "Default/Sessions", "Default/Sessions_Encrypted"):
        path = dest / leftover
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    _prefs_set_restore_new_tab(dest / "Default/Preferences")
    return dest


def _prefs_set_restore_new_tab(prefs: Path) -> None:
    """Set session.restore_on_startup=5 (New Tab page) in the copied preferences."""
    import json as _json

    try:
        data = _json.loads(prefs.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt prefs are not fatal
        return
    profile = data.setdefault("profile", {})
    if profile.get("restore_on_startup") == 5:
        return
    profile["restore_on_startup"] = 5
    profile.pop("exit_type", None)
    try:
        prefs.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- launch
def port_in_use(port: int) -> bool:
    """True if something already listens on 127.0.0.1:port.

    A leftover Edge from a crashed run would otherwise answer our devtools calls
    while the freshly launched browser silently failed to bind the port - and we
    would drive the wrong profile.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def launch(profile: Path, port: int, headless: bool = False) -> subprocess.Popen:
    if not Path(EDGE_APP).exists():
        raise FileNotFoundError(f"Microsoft Edge not found at {EDGE_APP}")
    args = [
        EDGE_APP,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2",
    ]
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- session
class Session:
    """Context manager yielding (cdp, org_uuid) against an authenticated browser."""

    def __init__(self, port: int = 9222, profile_copy: Path | None = None,
                 source_profile: Path | None = None, keep_profile: bool = False,
                 org: str | None = None, headless: bool = False, quiet: bool = False):
        self.port = port
        # Per-port default: two concurrent runs must not share (and rmtree) the
        # same copied profile.
        default_copy = Path(tempfile.gettempdir()) / f"claude-export-profile-{port}"
        self.profile_copy = Path(profile_copy) if profile_copy else default_copy
        self.source_profile = source_profile
        self.keep_profile = keep_profile
        self.org = org
        self.headless = headless
        self.quiet = quiet
        self.proc: subprocess.Popen | None = None
        self.cdp: CDP | None = None

    def log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr, flush=True)

    def __enter__(self) -> tuple[CDP, str]:
        # If anything below raises, __exit__ is NOT called by the with-statement,
        # so clean up explicitly or a 40MB profile copy (with your cookie DB)
        # would be left behind plus a stray Edge process.
        if port_in_use(self.port):
            raise RuntimeError(
                f"port {self.port} is already in use - another Edge/devtools session is "
                f"running. Close it or pass --port <other>."
            )
        try:
            self.log(f"copying profile -> {self.profile_copy}")
            profile = copy_profile(self.profile_copy, self.source_profile, quiet=self.quiet)
            self.log("launching Edge with devtools")
            self.proc = launch(profile, self.port, headless=self.headless)
            self.cdp = CDP(browser_ws(self.port))
            tid, sess = self.cdp.new_page()
            self.cdp.navigate(sess, "https://claude.ai/", settle=5)
            status, payload = self.cdp.fetch_json(sess, "/api/organizations")
            self.cdp.close_page(tid)
            if status != 200 or not isinstance(payload, list):
                raise RuntimeError(
                    f"not authenticated (got {status}). Sign in to claude.ai in Edge, "
                    "quit Edge completely, then retry."
                )
            if not payload:
                raise RuntimeError("this account returned no organizations")
        except BaseException:
            try:
                self._teardown()
            except Exception as cleanup_err:  # noqa: BLE001
                self.log(f"warning: cleanup failed: {cleanup_err}")
            raise
        org = self.org or payload[0]["uuid"]
        self.log(f"authenticated as org {org}")
        return self.cdp, org

    def __exit__(self, *exc) -> None:
        self._teardown()

    def _teardown(self) -> None:
        """Close CDP, stop Edge, and remove the profile copy (idempotent)."""
        if self.cdp:
            self.cdp.close()
            self.cdp = None
        stop(self.proc)
        self.proc = None
        if self.keep_profile:
            if self.profile_copy.exists():
                self.log(f"profile copy kept at {self.profile_copy} (contains your cookie DB)")
            return
        # Edge may still be flushing when we remove the tree; retry before giving up.
        for _ in range(6):
            shutil.rmtree(self.profile_copy, ignore_errors=True)
            if not self.profile_copy.exists():
                return
            time.sleep(1)
        self.log(
            f"warning: could not fully remove {self.profile_copy} - it contains a copy of "
            "your cookie database, delete it manually"
        )
