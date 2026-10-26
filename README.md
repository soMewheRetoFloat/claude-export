# claude-export

> **macOS only.** The tool drives the local Microsoft Edge app through its
> profile and Keychain-free session; there is no Windows/Linux path.

Export **your own** [claude.ai](https://claude.ai) data — conversations, generated files, standalone artifacts and attachments — by reusing the session that already exists in your local Microsoft Edge profile.

Useful when the web UI's *Export data* is unavailable (suspended / held account, billing lock, region issues) but the browser still holds a working session.

> Works on macOS. Uses your own logged-in Edge profile, and nothing is sent anywhere except to claude.ai itself.
>
> **No Keychain access.** The tool never asks macOS for your login keychain and never
> decrypts cookies itself — it launches Edge against a copy of your profile and lets the
> browser use its own session. You should see no security prompt at any point.

## What you get

| Output | Contents |
| --- | --- |
| `export/markdown/` | one readable Markdown file per conversation |
| `export/artifacts/` | every file Claude generated inside chats (`create_file`, `Write`, widget HTML, native artifacts) |
| `export/artifacts_live/` | standalone artifacts at `claude.ai/artifact/<id>` → `content.md` (via the viewer's own Export → Markdown); `content.pdf` kept as a visual copy when present |
| `export/artifacts_local/` | artifact `.md` files the API cannot reach — ones you uploaded or were shared with — filed by `import-local` |
| `export/files/` | attachments: images (webp previews) and documents (PDF) |
| `raw/conversations_full/` | original JSON, complete message tree incl. `thinking`, `tool_use`, `tool_result` |
| `export/README.md` | generated index of everything above |

## Install

```bash
git clone https://github.com/<you>/claude-export.git
cd claude-export
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# PDF text extraction (for artifacts)
brew install poppler
```

## Use

1. Open claude.ai in **Microsoft Edge** and make sure you are signed in.
2. Edge **may stay open** — the tool copies your profile to a temp directory and
   launches a second instance next to yours. Quitting it first is optional and only
   makes the copy slightly more consistent.
3. Run (no keychain / admin prompt should appear):

```bash
python3 -m claude_export doctor          # verify profile / cookies / login
python3 -m claude_export all             # everything, into ./export
```

Or step by step, which is what you want if you hit rate limits:

```bash
python3 -m claude_export conversations   # ~1.5s per conversation
python3 -m claude_export artifacts       # generated files + standalone artifacts (~10s each)
python3 -m claude_export artifacts --force  # re-capture even if output exists
python3 -m claude_export attachments     # ~2.5s per file
python3 -m claude_export render          # offline
python3 -m claude_export index           # offline
python3 -m claude_export import-local    # offline: file downloaded artifact .md files
```

Every command is resumable: existing files are skipped, so a killed run can be restarted.

Useful flags:

Options may go before or after the subcommand (`doctor --out x` works). `all` runs
every network step in a **single** browser session, then renders and indexes.

```
--out DIR            output directory (default: export)
--pace SECONDS       delay between requests (default 2.5; raise it if you get 403s)
--limit N            only process N items
--from DIR           import-local: source directory (default: ~/Downloads)
--move               import-local: delete the source after a successful import
--org UUID           organization uuid (default: the first one on the account)
--keep-profile       keep the temporary profile copy (debugging)
--headless           run Edge headless (verified on Edge 156)
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

51 tests, no network and no browser needed. They cover argument parsing (including
flags placed after the subcommand), the request expressions, attachment filename
collisions, artifact-id harvesting, Markdown rendering and the README index.

Most of them exist because of bugs that actually bit during development: a failed login
must not leave the profile copy behind, an expired session must fail fast instead of
retrying, tool-supplied paths must not escape the export directory, a half-written file
must never be mistaken for a complete one, links must survive spaces/parentheses and a
moved export directory, a limited run must not shrink the index, and a partially rendered
artifact must not be mistaken for a finished capture.

## Artifacts the API cannot reach

Some artifacts never appear in a conversation or in any id you can harvest — ones you
uploaded yourself, or that someone shared with you. When you download those by hand you
end up with a `.md` in `~/Downloads`. `import-local` files them into the export:

```bash
python3 -m claude_export import-local                  # from ~/Downloads
python3 -m claude_export import-local --from ~/Desktop --move
```

Imports are keyed by content hash (re-running is a no-op) and recorded in
`export/artifacts_local.json` with the source path and sha256. A local copy that happens
to be byte-identical to an API export is kept and marked `duplicate_of` — old revisions
are data too.

## How it works

Three non-obvious things make this work; all are documented in [`docs/API-NOTES.md`](docs/API-NOTES.md).

**1. Profile copy, not cookie injection.** claude.ai binds a session to more than the `sessionKey` cookie — profile-level state (localStorage, IndexedDB, device ids) participates too. Injecting cookies into a clean profile yields `403 → /login`. Copying the real profile and launching Edge against the copy works. Edge also refuses `--remote-debugging-port` on the default profile directory (Chromium 136+), so a copy is required anyway.

**2. The full message tree needs explicit query params.** Without `?tree=True&rendering_mode=messages&render_all_tools=true` the conversation endpoint flattens `tool_use` / `tool_result` into plain prose, and every generated file silently disappears.

**3. Artifacts are exported, not scraped.** The viewer has its own *Export → Markdown*,
and the file is generated server-side. That is faster than rendering (the menu works ~7s
after navigation, before the body paints), more faithful (tables, headings and lists
survive, and no page boundary can clip text), and it avoids PDF pagination entirely.
PDF is only a fallback for the few artifacts that do not offer Markdown, and those are
captured by sampling until the extracted text stops growing.

**4. Artifact bodies are not fetchable as files.** A standalone artifact's content is pushed over a WebSocket into a cross-origin iframe (`*.frame.claudeusercontent.com`), so neither the JSON API nor an MHTML snapshot contains it. `Page.printToPDF` composites the iframe and is the only reliable capture; text is then recovered with `pdftotext`.

## Caveats

- Do not point two concurrent runs at the same `--out` directory: each run is isolated
  (own Edge instance, own profile copy per port, own download dir) but the index files
  are last-write-wins.
- If a run was killed, an Edge instance can keep holding the devtools port. The tool
  refuses to start against a busy port (it would be driving the wrong browser); close that
  Edge, or pass `--port`.
- **Pace yourself.** Defaults are deliberately slow. Hammering the API can invalidate the session; if you start getting `403`, stop, wait, and raise `--pace`. An expired session now aborts immediately with a clear message instead of retrying each item.
- Image attachments are only served as 1204px webp previews — the original endpoint does not exist.
- Files produced by code execution (`blob` kind: tarballs, pptx, py, txt) are ephemeral and often already `404` by the time you look. They are listed in `export/files_missing.json` rather than silently dropped.
- A held account may lose access mid-run. Run `conversations` first: it is the part that cannot be reconstructed from anything else.
- This is a personal backup tool, not a scraper for other people's data. Only run it against accounts you own.

## License

MIT — see [LICENSE](LICENSE).

---

## 中文说明

把你自己的 claude.ai 数据（对话、对话内生成的文件、独立 Artifact、附件）通过本机 Edge 的登录态导出来。适合网页端「Export data」用不了的情况，比如账号被 hold。

```bash
# 1. 在 Edge 里确认已登录 claude.ai
# 2. 完全退出 Edge（⌘Q）
python3 -m claude_export doctor     # 自检
python3 -m claude_export all        # 全量导出到 ./export
```

三个关键点：

1. **必须复制整个 profile，不能只注入 Cookie**——claude.ai 会校验 profile 里的设备状态；而且 Edge 136+ 禁止在默认 profile 上开调试端口。
2. **对话必须带 `?tree=True&rendering_mode=messages&render_all_tools=true`**，否则工具调用被压平成纯文本，生成的文件全部丢失。
3. **独立 Artifact 的正文只能用 `printToPDF` 抓**——它是服务端通过 WebSocket 注入跨域 iframe 的，JSON 和 MHTML 里都没有。

默认限速偏保守；如果出现 403，停下来等一会儿再调大 `--pace`。会话失效会立即中止并提示重新登录，不会逐条重试。

自测：`python3 -m unittest discover -s tests -v`（19 个用例，不需要联网和浏览器）。图片附件只有 webp 预览版；代码执行产生的临时文件往往已过期（404），会记到 `files_missing.json` 里而不会静默丢掉。
