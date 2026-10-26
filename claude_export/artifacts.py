"""Collect artifact bodies.

Two different things are called "artifact" in claude.ai:

1. Content Claude generated *inside* a conversation (`create_file`, `Write`,
   widget code, the native `artifacts` tool). Those payloads are already in the
   conversation JSON and `extract_from_conversations` writes them out as files.

2. Standalone artifacts reachable at claude.ai/artifact/<id>. Their body is
   injected over a WebSocket into a cross-origin iframe, so neither the API JSON
   nor an MHTML snapshot contains it. `printToPDF` composites the iframe, which
   is the only reliable way to capture the rendered body; text is then pulled
   back out with pdftotext.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path

from .cdp import CDP
from .conversations import SessionExpired, load_local

# Indirection so tests can drive time deterministically instead of sleeping.
_sleep = time.sleep
_now = time.monotonic
from .util import safe_relative, write_atomic

ARTIFACT_URL = re.compile(r"(?:claude\.ai)?/(?:code/)?artifact/([A-Za-z0-9_-]{10,})")


# ------------------------------------------------------------ inside chats
def _slug(s: str, n: int = 60) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip()
    s = re.sub(r"[^\w\u4e00-\u9fff\-.]+", "_", s)
    return (s or "untitled")[:n].strip("_") or "untitled"


def extract_from_conversations(convs_dir: Path, out: Path) -> list[dict]:
    """Write every generated file / artifact / widget found in chat tool calls."""
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    written: dict[Path, str] = {}      # path -> content hash written during this run

    for conv in load_local(convs_dir):
        cid = (conv.get("uuid") or "")[:8]
        title = conv.get("name") or "untitled"
        cdir = out / f"{cid}_{_slug(title, 40)}"
        for msg in conv.get("chat_messages") or []:
            for block in msg.get("content") or []:
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                inp = block.get("input") or {}
                ts = (msg.get("created_at") or "")[:19].replace(":", "")

                payload = rel = None
                if name == "artifacts" and inp.get("content"):
                    ext = {"text/markdown": ".md", "text/html": ".html"}.get(inp.get("type"), ".txt")
                    rel = f"artifact_{ts}_{_slug(inp.get('title'))}{ext}"
                    payload = inp["content"]
                elif name in ("create_file", "Write") and (inp.get("file_text") or inp.get("content")):
                    rel = (inp.get("path") or inp.get("file_path") or "file.txt").lstrip("/")
                    payload = inp.get("file_text") or inp.get("content")
                elif name == "visualize:show_widget" and inp.get("widget_code"):
                    rel = f"widget_{ts}_{_slug(inp.get('title'))}.html"
                    payload = inp["widget_code"]
                if not payload or not rel:
                    continue

                dest = safe_relative(cdir, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload, ensure_ascii=False, indent=1)

                # Idempotent: the same source path must land on the same file, so
                # re-running the step overwrites instead of piling up _1, _2, ...
                # A suffix is only needed when *this run* produces two different
                # payloads for one path.
                digest = hashlib.sha256(payload.encode()).hexdigest()
                base = dest
                i = 1
                while written.get(dest) not in (None, digest):
                    dest = base.with_name(f"{base.stem}_{i}{base.suffix}")
                    i += 1
                written[dest] = digest
                write_atomic(dest, payload)
                manifest.append({"conv": title, "tool": name, "file": str(dest), "bytes": len(payload.encode())})

    write_atomic(out.parent / "artifacts_manifest.json",
                 json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"extracted {len(manifest)} generated files")
    return manifest


def collect_ids(convs_dir: Path) -> list[str]:
    """Artifact ids referenced anywhere in the conversations."""
    ids: set[str] = set()
    for conv in load_local(convs_dir):
        for msg in conv.get("chat_messages") or []:
            for block in msg.get("content") or []:
                if block.get("type") == "tool_use":
                    for m in ARTIFACT_URL.finditer(json.dumps(block.get("input"), ensure_ascii=False)):
                        ids.add(m.group(1))
                elif block.get("type") == "tool_result":
                    cc = block.get("content")
                    text = " ".join(x.get("text", "") for x in cc if isinstance(x, dict)) if isinstance(cc, list) else str(cc)
                    for m in ARTIFACT_URL.finditer(text):
                        ids.add(m.group(1))
    # share-style ids (nanoid) first - they tend to resolve
    return sorted(ids, key=lambda x: (0 if not re.match(r"^[0-9a-f]{8}-", x) else 1))


# ------------------------------------------------------------ standalone
def _click_js(pattern: str, scope: str = "document") -> str:
    return (
        "(()=>{const d=%s;const re=new RegExp(%s,'i');"
        "const b=[...d.querySelectorAll('button,[role=menuitem]')].find(x=>re.test((x.innerText||'').trim()));"
        "if(!b)return 0;b.click();return 1})()"
    ) % (scope, json.dumps(pattern))


_MENU_READY_JS = (
    "(()=>!![...document.querySelectorAll('button,[role=menuitem]')]"
    ".find(x=>/^Markdown/i.test((x.innerText||'').trim())))()"
)


_DIALOG_TEXT = ("(()=>{const d=document.querySelector('[role=dialog],dialog');"
                "return d?d.innerText.slice(0,200):''})()")


def _dismiss_dialog(cdp: CDP, sess: str) -> str:
    """Close whatever modal is open (e.g. the Sharing sheet opened by a stray click)."""
    text = cdp.eval(sess, _DIALOG_TEXT) or ""
    if not text:
        return ""
    if "Save file" in text or "Save this file" in text:
        return text  # Escape here would cancel the download we are waiting for
    for key in ("Escape", "Escape"):
        try:
            cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key,
                                                "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27},
                     session=sess)
            cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key,
                                                "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27},
                     session=sess)
        except Exception:  # noqa: BLE001
            break
        _sleep(0.4)
        if not (cdp.eval(sess, _DIALOG_TEXT) or ""):
            break
    return text


def _open_export_menu(cdp: CDP, sess: str, timeout: float = 22.0) -> bool:
    """Open the Export menu without fighting the UI.

    The toolbar settles late: clicking too early is swallowed (or opens the sharing
    sheet instead), and repeated rapid clicks leave a modal on top so every later
    click misses. So: wait for the toolbar rect to be stable, click once, check,
    clear any stray dialog, and only then try again - with a slow, bounded retry.
    """
    # wait until the Export control stops moving
    stable = 0
    last_rect = None
    deadline = _now() + timeout
    while _now() < deadline:
        rect = cdp.eval(sess, "(()=>{const b=[...document.querySelectorAll('button')]"
                              ".find(x=>/Export/.test(x.innerText||''));"
                              "if(!b)return null;const r=b.getBoundingClientRect();"
                              "return [r.x,r.y,r.width,r.height].map(Math.round).join(',')})()")
        if rect:
            stable = stable + 1 if rect == last_rect else 0
            last_rect = rect
            if stable >= 2:
                break
        _sleep(0.5)

    attempts = 0
    while _now() < deadline and attempts < 4:
        attempts += 1
        _dismiss_dialog(cdp, sess)
        if cdp.eval(sess, _MENU_READY_JS):
            return True
        cdp.eval(sess, _click_js(r"Export"))
        for _ in range(6):           # give the menu up to ~2.4s to appear
            _sleep(0.4)
            if cdp.eval(sess, _MENU_READY_JS):
                return True
        stray = _dismiss_dialog(cdp, sess)   # likely the sharing sheet
        if stray:
            _sleep(0.8)
    return cdp.eval(sess, _MENU_READY_JS)


def _wait_click(cdp: CDP, sess: str, pattern: str, timeout: float = 8.0,
                scope: str = "document") -> bool:
    """Click the first matching control, waiting for it to render."""
    deadline = _now() + timeout
    while _now() < deadline:
        if cdp.eval(sess, _click_js(pattern, scope)):
            return True
        _sleep(0.4)
    return False


def export_markdown(cdp: CDP, aid: str, dest: Path, download_dir: Path,
                    timeout: float = 40.0) -> Path | None:
    """Use the viewer's own Export -> Markdown, which is generated server-side.

    This is both faster (the button exists before the page finishes rendering) and
    more faithful than printing to PDF: tables, headings and lists survive, and no
    page boundary can clip the text.
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    # one directory per artifact: if a previous export's download finishes late it
    # lands in *its* directory, not this one
    work = download_dir / re.sub(r"[^\w.-]+", "_", aid)[:60]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:  # self-sufficient: capture() also sets this, but direct callers may not
        cdp.send("Browser.setDownloadBehavior",
                 {"behavior": "allow", "downloadPath": str(work), "eventsEnabled": True})
    except Exception:  # noqa: BLE001 - older protocol versions
        pass
    before = set(work.glob("*.md"))

    tid, sess = cdp.new_page()
    try:
        cdp.navigate(sess, f"https://claude.ai/artifact/{aid}")

        if not _open_export_menu(cdp, sess):
            return None
        if not _wait_click(cdp, sess, r"^Markdown", timeout=3):
            return None

        deadline = _now() + timeout
        next_retry = 0.0
        while _now() < deadline:
            _sleep(0.7)
            new = set(work.glob("*.md")) - before
            if new:
                f = max(new, key=lambda p: p.stat().st_size)
                size = f.stat().st_size
                _sleep(0.8)
                if f.exists() and f.stat().st_size == size and size > 0:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), dest)
                    return dest

            dialog = cdp.eval(sess, _DIALOG_TEXT) or ""
            if "Save file" in dialog:
                cdp.eval(sess, _click_js(r"Save file",
                                         "document.querySelector('[role=dialog],dialog')||document"))
            elif "Got it" in dialog:
                cdp.eval(sess, _click_js(r"Got it",
                                         "document.querySelector('[role=dialog],dialog')||document"))
                _open_export_menu(cdp, sess, timeout=10)
                _wait_click(cdp, sess, r"^Markdown", timeout=4)
            elif "export a copy" in dialog or "Sharing isn" in dialog:
                # This account gets "Sharing isn't included in your plan, but you
                # can export a copy" - the export buttons live inside that sheet,
                # so click Markdown within the dialog scope instead of the page.
                cdp.eval(sess, _click_js(
                    r"^Markdown", "document.querySelector('[role=dialog],dialog')||document"))
            elif _now() >= next_retry:
                # a click can be swallowed (handler not ready yet, or the sheet it
                # opened is a different layer). Re-run the whole sequence, slowly.
                next_retry = _now() + 5.0
                if _open_export_menu(cdp, sess, timeout=8):
                    _wait_click(cdp, sess, r"^Markdown", timeout=4)
        return None
    finally:
        cdp.close_page(tid)


def pdf_text(pdf: Path) -> str:
    if not shutil.which("pdftotext"):
        return ""
    try:
        r = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, timeout=90)
        return r.stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _capture_pdf(cdp: CDP, sess: str, dest: Path, retries: int, min_chars: int) -> tuple[int, bytes]:
    """Progressive-render fallback: sample until the text stops growing."""
    pdf_path = dest / "content.pdf"
    best_len, best_pdf, last_len = -1, b"", -1
    for attempt in range(retries):
        _sleep(12 if attempt == 0 else 6)
        pdf = cdp.print_pdf(sess)
        candidate = dest / f".try{attempt}.pdf"
        try:
            write_atomic(candidate, pdf)
            n = len(pdf_text(candidate).strip())
        finally:
            candidate.unlink(missing_ok=True)
        if n > best_len:
            best_len, best_pdf = n, pdf
        if n >= min_chars and n == last_len:
            break
        last_len = n
    write_atomic(pdf_path, best_pdf)
    return best_len, best_pdf


def capture(cdp: CDP, ids: list[str], out: Path, pace: float = 3.0,
            min_chars: int = 400, retries: int = 6, force: bool = False,
            pdf_only: bool = False) -> list[dict]:
    """Save each standalone artifact.

    Preferred route is the viewer's Export -> Markdown (server-generated, no
    pagination, tables intact, available before the body renders). PDF is the
    fallback for artifacts that do not offer it.
    """
    out.mkdir(parents=True, exist_ok=True)
    if not shutil.which("pdftotext"):
        print("warning: pdftotext not found (brew install poppler) - PDF fallback "
              "will produce no text", flush=True)

    dl_dir = Path(tempfile.mkdtemp(prefix="claude-export-dl-"))
    results: list[dict] = []
    try:
        try:
            cdp.send("Browser.setDownloadBehavior",
                     {"behavior": "allow", "downloadPath": str(dl_dir), "eventsEnabled": True})
        except Exception:  # noqa: BLE001 - older browsers
            pass

        for i, aid in enumerate(ids, 1):
            dest = out / aid
            dest.mkdir(parents=True, exist_ok=True)
            md_path = dest / "content.md"
            txt_path = dest / "content.txt"

            # markdown output only needs a few lines (app artifacts are short),
            # a PDF capture needs enough text to prove it rendered
            existing = md_path if md_path.exists() else (txt_path if txt_path.exists() else None)
            if not force and existing is not None:
                have = len(existing.read_text(encoding="utf-8").strip())
                need = 100 if existing == md_path else min_chars
                if have >= need:
                    results.append({"id": aid, "chars": have, "skipped": True})
                    continue

            try:
                md = None if pdf_only else export_markdown(cdp, aid, md_path, dl_dir)
                chars = len(md.read_text(encoding="utf-8").strip()) if md else 0

                if chars >= 100:
                    write_atomic(txt_path, md.read_text(encoding="utf-8"))
                    write_atomic(dest / "meta.json",
                                 json.dumps({"id": aid, "chars": chars, "format": "markdown"},
                                            ensure_ascii=False, indent=1))
                    print(f"[{i}/{len(ids)}] {aid[:12]} md={chars} chars", flush=True)
                    results.append({"id": aid, "chars": chars, "format": "markdown"})
                    continue

                old_txt = txt_path.read_text(encoding="utf-8").strip() if txt_path.exists() else ""
                if (dest / "content.pdf").exists() and len(old_txt) >= min_chars:
                    # a complete PDF capture already exists; re-rendering it would
                    # cost ~40s and change nothing
                    print(f"[{i}/{len(ids)}] {aid[:12]} kept existing pdf capture", flush=True)
                    results.append({"id": aid, "chars": len(old_txt), "kept": True})
                    continue

                tid, sess = cdp.new_page()
                try:
                    cdp.navigate(sess, f"https://claude.ai/artifact/{aid}")
                    _sleep(3)
                    if "/login" in (cdp.eval(sess, "location.pathname") or ""):
                        raise SessionExpired("redirected to /login - re-login in Edge and retry")
                    n, pdf = _capture_pdf(cdp, sess, dest, retries, min_chars)
                    text = pdf_text(dest / "content.pdf")
                    write_atomic(txt_path, text)
                    write_atomic(dest / "meta.json",
                                 json.dumps({"id": aid, "chars": len(text.strip()), "format": "pdf"},
                                            ensure_ascii=False, indent=1))
                    print(f"[{i}/{len(ids)}] {aid[:12]} pdf={len(pdf)//1024}KB text={len(text.strip())}",
                          flush=True)
                    results.append({"id": aid, "chars": len(text.strip()), "format": "pdf"})
                finally:
                    cdp.close_page(tid)
            except SessionExpired:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(ids)}] FAIL {aid[:12]}: {e}", flush=True)
                results.append({"id": aid, "error": str(e)[:200]})

            _sleep(pace)
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)
    return results


def verify(out: Path) -> list[tuple[str, int]]:
    """Report extractable text length for every captured artifact."""
    rows = []
    for d in sorted(out.glob("*")):
        for candidate in (d / "content.md", d / "content.txt"):
            if candidate.exists():
                rows.append((d.name, len(candidate.read_text(encoding="utf-8").strip())))
                break
    return rows
