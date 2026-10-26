# Reverse-engineered notes

Everything here was observed against `claude.ai` while exporting a personal account.
Endpoints are **internal and unversioned** — they change without notice. Treat this as
documentation of a moving target, not a stable API.

## Auth

- Session cookie: `.claude.ai / sessionKey`, plus `sessionKeyV3` siblings.
- A valid cookie alone is **not** enough. In a fresh Chromium profile with only claude.ai
  cookies copied in, every call returned `403` and the app redirected to `/login`.
  The same cookie inside a copied real profile returned `200`.
  Conclusion: profile-scoped state is part of the session check.
- Edge/Chromium ≥ 136 refuses `--remote-debugging-port` when `--user-data-dir` is the
  default profile. Workaround used here: `rsync` the profile elsewhere (minus caches) and
  launch against the copy.
- **Cookie values never need to be decrypted.** Launching Edge against a profile copy lets
  the browser present its own authenticated session, so the tool touches neither the Keychain
  nor the encrypted `encrypted_value` blobs. It only counts cookie *names* for `doctor`.
  (Decryption would be `PBKDF2-HMAC-SHA1(keychain, "saltysalt", 1003, 16)` → AES-128-CBC,
  IV = 16 spaces, plaintext prefixed with `SHA256(host_key)` on schema v24+. Kept here only as
  a note; nothing in this repo does it.)

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/organizations` | first entry's `uuid` is the org used everywhere; also exposes `api_disabled_reason` |
| GET | `/api/organizations/{org}/chat_conversations` | list; `?limit=&offset=` for paging |
| GET | `/api/organizations/{org}/chat_conversations_v2` | `{data, has_more}` variant |
| GET | `/api/organizations/{org}/chat_conversations/{uuid}` | flattened messages only |
| GET | `/api/organizations/{org}/chat_conversations/{uuid}?tree=True&rendering_mode=messages&render_all_tools=true` | **full tree** — `content[]` with `text` / `thinking` / `tool_use` / `tool_result` |
| GET | `/api/organizations/{org}/projects` · `/projects/{uuid}` · `/projects/{uuid}/docs` | project + knowledge docs |
| GET | `/api/organizations/{org}/shares` | shared items (empty on a private account) |
| GET | `/api/frame/{artifact_id}?org={org}&via=user_open&bk=initial&actor=id&vt=1&rw=1` | artifact manifest: title, `ver`, `files[]` with `path`/`size`/`sha256` |

Notes:

- `render_all_tools=true` is what materializes tool calls. Without it the payload is ~5×
  smaller and contains no `tool_use` at all.
- There is **no** `/artifacts` listing endpoint; artifact ids have to be harvested from
  conversation `tool_use` inputs and `tool_result` texts
  (`https://claude.ai/artifact/<id>`, `https://claude.ai/code/artifact/<uuid>`).
- The same artifact is often reachable under two ids (uuid and a nanoid share id); dedupe
  by content hash, not by id.

## Artifact bodies

The manifest is metadata only: `files[].src = {slug, ver}`, and the file server answers every
guessable path (`/_f/<ver>/index.html`, `SKILL.md`, …) with the SPA shell. The actual body is
delivered over a WebSocket lane (`/api/frame/lane/bongo/<id>/subscribe`) and painted into a
cross-origin iframe on `<id>.frame.claudeusercontent.com`.

Consequences:

- `Page.captureSnapshot` (MHTML) captures the iframe *document*, which is the runtime shell —
  ~0 CJK characters for a Chinese artifact. Useless here.
- `Page.printToPDF` composites the whole page including the iframe → the real content.
  Then `pdftotext` recovers the text.
- Interactive/app-type artifacts still yield little text (a canvas has no text). Capture them
  as PDF and accept that; don't treat a short extraction as a failure.

## Export → Markdown (preferred route)

The artifact viewer's *Export* menu offers Word / PDF / **Markdown** / Google Docs / Notion.
Markdown is generated server-side, so it is available **~7s after navigation, before the
body renders**, and it is the cleanest capture: 30,363 chars for an artifact whose PDF
text extraction yields 28,127.

Timeline that matters:

| t | state |
| --- | --- |
| ~3s | Export button exists in DOM but its handler is not wired yet; clicking does nothing |
| ~5s | click registers, menu still empty |
| ~7s | menu items (Word/PDF/Markdown/…) appear |
| after click | a `Save this file?` dialog, sometimes a `Duplicate this artifact · Got it` tip |

The file arrives as a download, so `Browser.setDownloadBehavior` must point at a scratch
directory. Names come from the artifact title (spaces, CJK, parentheses) — rename on move.

### Three ways this silently fails

1. **Clicking too early.** The Export button is in the DOM at ~3s but its handler is only
   wired around ~7s, so early clicks are swallowed. Hammering the button makes it worse.
   Wait until the button's bounding rect is stable for two consecutive samples.
2. **A different sheet opens instead of the menu.** Accounts without sharing get
   *"Sharing isn't included in your plan, but you can export a copy"* — that sheet contains
   Word/PDF/Markdown itself. For such accounts Export always leads there; it is not a
   mis-click. The export option has to be clicked from inside the dialog scope, and the
   first click may only bring the sheet up.
   Note: none of the selectors used here (`Export`, `^Markdown`, `Save file`, `Got it`)
   can hit Share / Copy link, so a run never creates a public link — verified against
   `/api/organizations/{org}/shares`, which stays empty.
3. **The "Save this file?" confirmation needs a real click, twice.** A single programmatic
   click is sometimes ignored, so the confirm must be retried until the download appears.

Because of these, the export routine retries the whole open→click→confirm cycle on a timer
instead of assuming one pass works.

## Artifacts missing from the API

Only artifacts referenced from a conversation (`tool_use` inputs / `tool_result` text) can
be discovered, and only those resolve through `/api/frame/`. Artifacts you uploaded
yourself, or that were shared with you, have no conversation pointing at them, so there is
no id to harvest. Downloading them by hand and importing the `.md` is the practical answer
(`import-local`).

## Profile handoff (why quitting Edge used to be required)

A copied profile still contains `SingletonLock` / `SingletonSocket` / `SingletonCookie`,
which point at the **live** browser's IPC endpoint. Launching against the copy then prints
"opening in existing browser session", exits, and never opens the devtools port. Deleting
those three links (root and `Default/`) lets the second instance start while your normal
Edge stays open.

## Progressive rendering (PDF fallback only)

The artifact viewer does not render in one shot. Measured on a 13-page artifact:

| time after navigate | PDF | pages | extracted text |
| --- | --- | --- | --- |
| 8s / 11s / 14s / 20s | 1-2KB | 1-2 | 0 chars |
| 28s | 485KB | 13 | 28,127 chars |

So a capture taken "when the page looks loaded" is a blank shell, and even a capture at
~25s can be a partial render (27.3k chars instead of 28.1k). The tool therefore samples
repeatedly and stops when the extracted text length stops changing, keeping the richest
PDF. Budget ~35s per artifact.

Also measured — the print box settings do **not** help:

| params | pages | text |
| --- | --- | --- |
| `preferCSSPageSize` + A4 | 12 | 27,567 |
| A4 only | 12 | 27,831 |
| `preferCSSPageSize` only | 13 | 28,127 |
| defaults | 13 | 28,127 |

Passing an explicit paper size clips a page boundary and loses ~2% of the text, so the
code calls `Page.printToPDF` with `printBackground` only.

## Attachments

Message objects carry `files[]` with `file_kind`:

| kind | URL | Notes |
| --- | --- | --- |
| `image` | `/api/{org}/files/{uuid}/preview` | only variant served; `/original`, `/image`, `/download` → 404. Content is webp ~1204px. |
| `document` | `/api/{org}/files/{uuid}/document_pdf` | the real PDF |
| `blob` | `/api/{org}/files/{uuid}?download_source=files-api` | code-execution output; frequently already 404 (ephemeral) |

Binary download through a page: `fetch` → `arrayBuffer` → base64 in `window.__dl` →
`Runtime.evaluate` slices. Avoids replaying cookies into curl (Cloudflare fingerprints
would block it anyway).

## Rate limiting

Bursty parallel fetching got the session rejected (`403`, redirect to `/login`) roughly
20 minutes in. A single-threaded run with ~2.5s between requests and ~10s per artifact page
ran clean for the whole export. Assume the limiter exists and is not generous.
