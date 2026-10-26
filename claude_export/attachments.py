"""Download attachments (images / documents / blobs) referenced by messages."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .cdp import CDP, is_context_lost
from .util import write_atomic
from .conversations import load_local


def collect(convs_dir: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """Returns ({file_uuid: entry}, {file_uuid: conversation name})."""
    entries: dict[str, dict] = {}
    conv_of: dict[str, str] = {}
    for conv in load_local(convs_dir):
        for msg in conv.get("chat_messages") or []:
            for f in msg.get("files") or []:
                if isinstance(f, dict) and f.get("file_uuid"):
                    entries.setdefault(f["file_uuid"], f)
                    conv_of.setdefault(f["file_uuid"], conv.get("name") or "")
    return entries, conv_of


def url_for(org: str, f: dict) -> str | None:
    kind = f.get("file_kind")
    if kind == "image":
        # only the preview variant is served; the original endpoint 404s
        return f.get("preview_url") or f.get("thumbnail_url")
    if kind == "document":
        return (f.get("document_asset") or {}).get("url")
    if kind == "blob":
        return f"/api/{org}/files/{f['file_uuid']}?download_source=files-api"
    return None


def _safe_name(name: str, uuid: str) -> str:
    """Server-supplied filename -> safe single path component."""
    base = Path(name.replace("\\", "/")).name.strip()
    base = base.replace("/", "_").replace("\0", "")
    if base in ("", ".", "..") or base.startswith("."):
        base = f"{uuid}{Path(base).suffix}" if base not in ("", ".", "..") else uuid
    return base


def plan(entries: dict[str, dict]) -> list[tuple[str, str, str]]:
    """(uuid, kind, filename) with collisions resolved by a uuid suffix."""
    used: set[str] = set()
    rows = []
    for uuid, f in entries.items():
        kind = f.get("file_kind") or "other"
        name = _safe_name(f.get("file_name") or uuid, uuid)
        stem, ext = Path(name).stem, Path(name).suffix
        if not ext:
            ext = {"image": ".webp", "document": ".pdf"}.get(kind, "")
        name = f"{stem}{ext}"
        if name in used:
            name = f"{stem}__{uuid[:8]}{ext}"
        used.add(name)
        rows.append((uuid, kind, name))
    return rows


def download(cdp: CDP, org: str, convs_dir: Path, out: Path, pace: float = 2.5,
             limit: int | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    entries, conv_of = collect(convs_dir)
    all_rows = plan(entries)
    # --limit only bounds the download loop; the index always covers every
    # known file, otherwise a limited run would erase earlier progress.
    rows = all_rows[:limit] if limit else all_rows

    tid = sess = None
    ok = failed = skipped = 0
    attempted: set[str] = set()
    try:
        tid, sess = cdp.new_page()
        cdp.navigate(sess, "https://claude.ai/", settle=4)
        for i, (uuid, kind, name) in enumerate(rows, 1):
            dest = out / kind / name
            if dest.exists() and dest.stat().st_size > 100:
                skipped += 1
                continue
            attempted.add(uuid)   # so a url-less entry still shows up as unavailable
            url = url_for(org, entries[uuid])
            if not url:
                failed += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = cdp.fetch_bytes(sess, url)
                if not data:
                    raise RuntimeError("empty body")
                write_atomic(dest, data)
                print(f"[{i}/{len(rows)}] {kind:8s} {len(data):>9d}B {name[:60]}", flush=True)
                ok += 1
            except Exception as e:  # noqa: BLE001
                if is_context_lost(e):
                    cdp.close_page(tid)
                    tid, sess = cdp.new_page()
                    cdp.navigate(sess, "https://claude.ai/", settle=4)
                    print(f"[{i}/{len(rows)}] reconnected after page reload", flush=True)
                    continue
                print(f"[{i}/{len(rows)}] FAIL {name[:50]}: {e}", flush=True)
                failed += 1
            time.sleep(pace)
    finally:
        if tid:
            cdp.close_page(tid)

    index, missing = [], []
    for uuid, kind, name in all_rows:
        p = out / kind / name
        if p.exists():
            index.append({"file_uuid": uuid, "kind": kind, "name": name,
                          "path": os.path.relpath(p, out.parent),
                          "size": p.stat().st_size, "conv": conv_of.get(uuid)})
        elif uuid in attempted:
            # only files we actually tried and failed on; a --limit run must not
            # label untouched files as "gone from the server"
            missing.append({"file_uuid": uuid, "kind": kind, "name": name,
                            "declared_bytes": entries[uuid].get("size_bytes"), "conv": conv_of.get(uuid)})

    write_atomic(out.parent / "files_index.json", json.dumps(index, ensure_ascii=False, indent=1))
    write_atomic(out.parent / "files_missing.json", json.dumps(missing, ensure_ascii=False, indent=1))
    print(f"\nattachments: {ok} downloaded, {skipped} skipped, {failed} failed, {len(missing)} unavailable")
    return {"ok": ok, "skipped": skipped, "failed": failed, "missing": len(missing)}
