"""Build README.md index for an export directory."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

from .util import write_atomic


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _rel(path: str | None, base: Path) -> str:
    """Link target relative to the export root.

    Three shapes exist in the wild and all must resolve from README.md:
      * absolute paths (first runs, different machine)
      * relative to the export root ("markdown/01_x.md")  <- current format
      * relative to the export's parent ("export/files/x") <- older exports
    Prefer whichever candidate actually exists on disk.
    """
    if not path:
        return ""
    p = Path(path)
    candidates: list[str] = []
    if p.is_absolute():
        candidates.append(os.path.relpath(p, base))
    else:
        candidates.append(str(p))
        if p.parts and p.parts[0] == base.name:
            candidates.append(str(Path(*p.parts[1:])))
    for c in candidates:
        if (base / c).exists():
            return c
    return candidates[0]


def _link(rel: str) -> str:
    """Percent-encode a relative path for use inside markdown `](...)`.

    Filenames with spaces or parentheses break the link syntax otherwise, and
    attachments routinely contain both.
    """
    return quote(rel, safe="/") if rel else ""


def _find_local_dir(export: Path, row: dict) -> str:
    """Locate the imported file for an index row (folder is name_hash)."""
    digest = row.get("sha256") or ""
    cand = export / "artifacts_local" / f"{_slugify(row.get('title'))}_{digest[:8]}" / "content.md"
    if cand.exists():
        return str(cand)
    for p in (export / "artifacts_local").glob(f"*_{digest[:8]}/content.md"):
        return str(p)
    return ""


def _slugify(text: str, n: int = 48) -> str:
    import re
    import unicodedata
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[^\w\u4e00-\u9fff\-.]+", "_", text)
    return (text or "artifact")[:n].strip("_") or "artifact"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def build(export: Path, org: str | None = None, site: str = "claude.ai") -> Path:
    convs = _load(export / "conversations_index.json", [])
    files = _load(export / "files_index.json", [])
    missing = _load(export / "files_missing.json", [])
    extracted = _load(export / "artifacts_manifest.json", [])
    imported = _load(export / "artifacts_local.json", [])

    arts = []
    live = export / "artifacts_live"
    if live.exists():
        for d in sorted(live.glob("*")):
            meta = d / "meta.json"
            pdf = d / "content.pdf"
            txt = d / "content.txt"
            m = {}
            if meta.exists():
                try:
                    m = json.loads(meta.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    m = {}
            elif (d / "manifest.json").exists():  # older layout
                try:
                    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                    m = {"title": man.get("title"), "chars": 0}
                except Exception:  # noqa: BLE001
                    m = {}
            if not pdf.exists():
                continue
            if txt.exists():
                m["chars"] = len(txt.read_text(encoding="utf-8").strip())
            arts.append({
                "id": d.name,
                "title": m.get("title") or d.name,
                "chars": m.get("chars", 0),
                "pdf": pdf.stat().st_size if pdf.exists() else 0,
            })

    total_msgs = sum(c.get("messages", 0) for c in convs)
    L = [
        f"# {site} 数据导出",
        "",
        f"- **导出时间**：{time.strftime('%Y-%m-%d %H:%M')}",
    ]
    if org:
        L.append(f"- **组织**：`{org}`")
    L += [
        "",
        "## 总量",
        "",
        "| 项目 | 数量 | 状态 |",
        "| --- | --- | --- |",
        f"| 对话（完整消息树） | {len(convs)} | ✅ |",
        f"| 消息 | {total_msgs} | ✅ |",
        f"| 对话内生成的产物 | {len(extracted)} | ✅ |",
        f"| 独立 Artifact（PDF + 正文） | {len(arts)} | ✅ |",
        f"| 本地导入的 Artifact | {len(imported)} | {'✅' if imported else '—'} |",
        f"| 附件 | {len(files)} | {'✅' if not missing else f'⚠️ 另有 {len(missing)} 个尝试后仍未取到'} |",
        "",
        "## 目录结构",
        "",
        "```",
        "export/",
        "├── README.md              # 本文件",
        "├── markdown/              # 每个对话的可读 Markdown",
        "├── artifacts/             # 对话内生成的文件 / artifact 正文 / widget",
        "├── artifacts_live/        # 独立 Artifact：content.pdf + content.txt + meta.json",
        "├── files/                 # 附件（图片、PDF 等）",
        "└── *.json                 # 各类索引",
        "raw/",
        "├── conversations_full/    # 原始完整对话 JSON",
        "└── projects/              # 项目及其文档（若导出）",
        "```",
        "",
        "## 对话",
        "",
        "| # | 标题 | 创建时间 | 消息数 | Markdown |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in convs:
        rel = _rel(c.get("md"), export)
        L.append(f"| {c['n']} | {c.get('name') or ''} | {c.get('created') or ''} | "
                 f"{c.get('messages', 0)} | [{rel}]({_link(rel)}) |")

    if arts:
        L += ["", "## 独立 Artifact", "",
              "> 正文由服务端经 WebSocket 注入跨域 iframe；这里用 `Page.printToPDF` 合成整页，"
              "再用 `pdftotext` 导出纯文本。",
              "", "| 标题 | ID | 正文字数 | 资源 |", "| --- | --- | --- | --- |"]
        for a in arts:
            L.append(
                f"| {a['title'][:50]} | `{a['id']}` | {a['chars']:,} | "
                f"[PDF](artifacts_live/{a['id']}/content.pdf) · [文本](artifacts_live/{a['id']}/content.txt) |"
            )

    if imported:
        L += ["", f"## 本地导入的 Artifact（{len(imported)} 个）", "",
              "> 这些不是这次通过接口抓的，是你自己下载或别人分享的 `.md`，"
              "由 `import-local` 归档。备注列会标出它与 API 版本的关系——"
              "有对应且更旧、内容已被重写、或根本不在归档里。",
              "", "| 标题 | 字数 | 导入时间 | 备注 | 文件 |", "| --- | --- | --- | --- | --- |"]
        for row in sorted(imported, key=lambda r: r.get("imported_at") or ""):
            if row.get("duplicate_of"):
                dup = f"与 API 版本逐字节相同（{row['duplicate_of'][:10]}）"
            elif (row.get("overlap") or 0) >= 0.9:
                dup = f"API 版本更全（{row['similar_to'][:10]}，本地 {row['overlap']:.0%} 重合）"
            elif (row.get("overlap") or 0) >= 0.3:
                dup = f"与 API 版本 {row['overlap']:.0%} 重合（{row['similar_to'][:10]}）"
            else:
                dup = "归档无对应（独立文档或已重写的旧版）"
            path = _find_local_dir(export, row)
            rel = _link(_rel(path, export)) if path else ""
            L.append(f"| {row.get('title', '')[:44]} | {row.get('chars', 0):,} | "
                     f"{(row.get('imported_at') or '')[5:16]} | {dup} | [content.md]({rel}) |")

    if files:
        L += ["", f"## 附件（{len(files)} 个）", "", "| 类型 | 名称 | 大小 | 来源对话 |", "| --- | --- | --- | --- |"]
        for f in files:
            rel = _link(_rel(f.get("path"), export))
            L.append(f"| {f['kind']} | [{f['name'][:70]}]({rel}) | {_human(f['size'])} | {(f.get('conv') or '')[:30]} |")

    if missing:
        L += ["", f"## 未能获取（{len(missing)} 个）", "",
              "> 这些文件下载时返回错误（多为代码执行产生的临时文件，服务端已 404）。",
              "", "| 类型 | 名称 | 声明大小 | 来源对话 |", "| --- | --- | --- | --- |"]
        for m in missing:
            L.append(f"| {m['kind']} | {m['name'][:60]} | {_human(m.get('declared_bytes') or 0)} | {(m.get('conv') or '')[:26]} |")

    L += [
        "",
        "## 方法与局限",
        "",
        "1. 通过本机 Edge 的登录态直接调用内部接口，走的是与网页相同的域名与 Cookie，未破解任何加密内容。",
        "2. 会话状态绑定 profile（localStorage / IndexedDB），单靠复制 Cookie 会被判定为未登录；"
        "本工具因此复制整个 profile 再启动。",
        "3. 对话使用 `?tree=True&rendering_mode=messages&render_all_tools=true` 拉取，否则 tool_use / tool_result 会被压平丢弃。",
        "4. 图片附件只有 preview 变体（webp），原图接口不存在。",
        "5. 建议同时使用官网的 Export data 作为交叉备份。",
        "",
    ]
    dest = export / "README.md"
    write_atomic(dest, "\n".join(L))
    print(f"wrote {dest} ({len(L)} lines)")
    return dest
