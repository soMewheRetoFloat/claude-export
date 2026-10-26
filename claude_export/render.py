"""Render conversations to readable Markdown."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

from .conversations import load_local
from .util import write_atomic


def _slug(s: str, n: int = 48) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip()
    s = re.sub(r"[^\w\u4e00-\u9fff\-.]+", "_", s)
    return (s or "untitled")[:n].strip("_")


def _ts(v: str | None) -> str:
    return (v or "")[:16].replace("T", " ")


def _block_text(c: dict, cap: int = 4000) -> str:
    t = c.get("type")
    if t == "tool_use":
        return json.dumps(c.get("input"), ensure_ascii=False, indent=1)[:cap]
    if t == "tool_result":
        cc = c.get("content")
        if isinstance(cc, list):
            return "\n".join(x.get("text", "") for x in cc if isinstance(x, dict))[:cap]
        return str(cc)[:cap]
    return c.get("text") or c.get("thinking") or ""


def render_conversation(conv: dict) -> str:
    lines = [
        f"# {conv.get('name') or 'untitled'}",
        "",
        f"- **conversation**: `{conv.get('uuid')}`",
        f"- **created**: {_ts(conv.get('created_at'))}   **updated**: {_ts(conv.get('updated_at'))}",
        f"- **model**: {conv.get('model')}   **starred**: {bool(conv.get('is_starred'))}",
        "",
        "---",
        "",
    ]
    for m in conv.get("chat_messages") or []:
        who = "👤 User" if m.get("sender") == "human" else "🤖 Assistant"
        lines += [f"## {who}  ·  {_ts(m.get('created_at'))}", ""]
        content = m.get("content")
        if content:
            for c in content:
                t = c.get("type")
                if t == "text":
                    if (c.get("text") or "").strip():
                        lines += [c["text"].strip(), ""]
                elif t == "thinking":
                    body = (c.get("thinking") or "").strip()
                    if body:
                        lines += ["<details><summary>thinking</summary>", "", body, "", "</details>", ""]
                elif t == "tool_use":
                    lines += [
                        f"<details><summary>🔧 tool · {c.get('name') or 'tool'}</summary>", "",
                        "```json", _block_text(c), "```", "", "</details>", "",
                    ]
                elif t == "tool_result" and c.get("is_error"):
                    lines += ["<details><summary>⚠️ tool error</summary>", "", "```", _block_text(c), "```", "", "</details>", ""]
        else:
            if (m.get("text") or "").strip():
                lines += [m["text"].strip(), ""]

        for f in m.get("files") or []:
            if isinstance(f, dict) and f.get("file_name"):
                lines.append(f"📎 attachment: `{f['file_name']}` ({f.get('file_kind')}, {f.get('size_bytes')} bytes)")
        lines.append("")
    return "\n".join(lines)


def render_all(convs_dir: Path, out: Path) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    convs = load_local(convs_dir)
    convs.sort(key=lambda c: c.get("created_at") or "")

    index = []
    for i, conv in enumerate(convs, 1):
        dest = out / f"{i:02d}_{_slug(conv.get('name'))}.md"
        md = render_conversation(conv)
        write_atomic(dest, md)
        index.append({
            "n": i,
            "name": conv.get("name"),
            "uuid": conv.get("uuid"),
            "created": _ts(conv.get("created_at")),
            "updated": _ts(conv.get("updated_at")),
            "messages": len(conv.get("chat_messages") or []),
            "md": os.path.relpath(dest, out.parent),
            "chars": len(md),
        })
        print(f"{i:02d} {len(md):>7d} chars  {(conv.get('name') or '')[:50]}")

    write_atomic(out.parent / "conversations_index.json",
                 json.dumps(index, ensure_ascii=False, indent=1))
    print(f"\nrendered {len(index)} markdown files")
    return index
