"""List and download conversations with the full message tree."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .cdp import CDP, is_context_lost
from .util import write_atomic

# Without these params the API flattens tool_use/tool_result into plain text and
# silently drops everything a message actually *did*.
FULL_QUERY = "?tree=True&rendering_mode=messages&render_all_tools=true"


class SessionExpired(RuntimeError):
    """The profile's session is no longer accepted (403 / redirected to /login)."""


def list_conversations(cdp: CDP, org: str) -> list[dict]:
    tid, sess = cdp.new_page()
    try:
        cdp.navigate(sess, "https://claude.ai/", settle=3)
        total: list[dict] = []
        seen: set[str] = set()
        offset = 0
        for _ in range(500):  # hard stop; the endpoint may ignore ?offset=
            path = f"/api/organizations/{org}/chat_conversations"
            if offset:
                path += f"?offset={offset}&limit=100"
            status, payload = cdp.fetch_json(sess, path)
            if status == 403:
                raise SessionExpired("http 403 listing conversations - re-login in Edge and retry")
            if status != 200 or not isinstance(payload, list):
                raise RuntimeError(f"conversation list failed: {status}")
            fresh = [c for c in payload if c.get("uuid") not in seen]
            for c in fresh:
                seen.add(c["uuid"])
            total.extend(fresh)
            # stop when the page is short, or when paging stopped advancing
            if len(payload) < 100 or not fresh:
                break
            offset = len(total)
        return total
    finally:
        cdp.close_page(tid)


def fetch_conversation(cdp: CDP, org: str, uuid: str, session: str) -> dict:
    status, payload = cdp.fetch_json(
        session, f"/api/organizations/{org}/chat_conversations/{uuid}{FULL_QUERY}"
    )
    if status == 403:
        raise SessionExpired("http 403 - session rejected; re-login in Edge and retry")
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"{uuid}: http {status}")
    return payload


def download_all(cdp: CDP, org: str, out: Path, pace: float = 1.5,
                 limit: int | None = None) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    all_convs = list_conversations(cdp, org)
    # --limit bounds the download loop only; the saved listing stays complete
    convs = all_convs[:limit] if limit else all_convs

    session = None
    tid = None
    done = skipped = failed = 0
    try:
        tid, session = cdp.new_page()
        cdp.navigate(session, "https://claude.ai/", settle=3)
        for i, cv in enumerate(convs, 1):
            uuid = cv["uuid"]
            dest = out / f"{uuid}.json"
            if dest.exists() and dest.stat().st_size > 500:
                skipped += 1
                continue
            for attempt in range(3):
                try:
                    data = fetch_conversation(cdp, org, uuid, session)
                    write_atomic(dest, json.dumps(data, ensure_ascii=False, indent=1))
                    n = len(data.get("chat_messages") or [])
                    print(f"[{i}/{len(convs)}] {uuid[:8]} msgs={n} {cv.get('name', '')[:40]!r}", flush=True)
                    done += 1
                    break
                except SessionExpired:
                    raise
                except Exception as e:  # noqa: BLE001
                    if is_context_lost(e) and attempt < 2:
                        cdp.close_page(tid)
                        tid, session = cdp.new_page()
                        cdp.navigate(session, "https://claude.ai/", settle=3)
                        print(f"[{i}/{len(convs)}] reconnected after page reload", flush=True)
                        continue
                    if attempt == 2:
                        print(f"[{i}/{len(convs)}] FAIL {uuid[:8]}: {e}", flush=True)
                        failed += 1
                    else:
                        time.sleep(1.5 * (attempt + 1))
            time.sleep(pace)
    finally:
        if tid:
            cdp.close_page(tid)

    write_atomic(out.parent / "conversations_list.json",
                 json.dumps(all_convs, ensure_ascii=False, indent=1))
    print(f"\nconversations: {done} new, {skipped} skipped, {failed} failed")
    return convs


def load_local(out: Path) -> list[dict]:
    """Read already-downloaded conversation JSON (for offline steps)."""
    rows = []
    broken = []
    for f in sorted(out.glob("*.json")):
        try:
            rows.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - corrupt/partial file
            broken.append(f.name)
    if broken:
        print(f"warning: {len(broken)} unreadable conversation file(s) skipped: "
              f"{', '.join(broken[:3])}{' ...' if len(broken) > 3 else ''}", file=sys.stderr)
    return rows
