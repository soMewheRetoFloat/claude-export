"""Smoke tests that do not touch the network or a browser.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_export import (artifacts, attachments, cdp, cli, conversations,  # noqa: E402
                           index, local_import, render, session, util)


class FakeClock:
    """Deterministic clock: sleep(seconds) advances time by exactly that much."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(float(seconds), 0.0)

    def patches(self):
        from unittest.mock import patch
        return [patch.object(artifacts, "_now", self.now),
                patch.object(artifacts, "_sleep", self.sleep)]


class FakeCDP(cdp.CDP):
    """CDP stand-in: records expressions, returns canned values."""

    def __init__(self, responder):
        self.responder = responder
        self.expressions: list[str] = []

    def eval(self, session, expr, await_promise=False):  # noqa: A003
        self.expressions.append(expr)
        return self.responder(expr)

    def new_page(self, url="about:blank"):
        return "target", "session"

    def close_page(self, target_id):
        pass

    def navigate(self, session, url, settle=0.0):
        pass


class ParserTests(unittest.TestCase):
    def test_all_accepts_artifact_flags(self):
        ap = cli.build_parser()
        ns = ap.parse_args(["all"])
        self.assertFalse(getattr(ns, "extract_only", False))
        self.assertEqual(getattr(ns, "min_chars", 400), 400)
        ns = ap.parse_args(["all", "--extract-only", "--min-chars", "800"])
        self.assertTrue(ns.extract_only)
        self.assertEqual(ns.min_chars, 800)

    def test_global_flag_after_subcommand(self):
        ap = cli.build_parser()
        self.assertEqual(ap.parse_args(["doctor", "--out", "x"]).out, "x")
        self.assertEqual(ap.parse_args(["--out", "y", "doctor"]).out, "y")

    def test_after_subcommand_does_not_clobber_globals(self):
        ap = cli.build_parser()
        ns = ap.parse_args(["--out", "y", "--pace", "9", "doctor", "--limit", "3"])
        self.assertEqual(ns.out, "y")
        self.assertEqual(ns.pace, 9)
        self.assertEqual(ns.limit, 3)

    def test_command_dispatch_table_matches_subparsers(self):
        ap = cli.build_parser()
        for name, _fn, _help, _flags in cli.COMMANDS:
            self.assertIn(name, ap._subparsers._group_actions[0].choices)  # noqa: SLF001


class PathTests(unittest.TestCase):
    def test_output_layout(self):
        p = cli._paths(Path("/tmp/work/export"))
        self.assertEqual(p["convs"], Path("/tmp/work/raw/conversations_full"))
        self.assertEqual(p["markdown"], Path("/tmp/work/export/markdown"))


class ExpressionTests(unittest.TestCase):
    def test_fetch_json_expression_is_balanced(self):
        def responder(expr):
            return json.dumps({"s": 200, "b": '{"a": 1}'})

        fake = FakeCDP(responder)
        status, payload = fake.fetch_json("s", "/api/x")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"a": 1})
        expr = fake.expressions[0]
        self.assertEqual(expr.count("{"), expr.count("}"))
        self.assertIn("/api/x", expr)

    def test_fetch_bytes_chunks_and_decodes(self):
        payload = b"hello world " * 100
        calls = []

        def responder(expr):
            calls.append(expr)
            if "arrayBuffer" in expr:
                return f"OK:{len(payload)}"
            if "__buf = null" in expr:
                return None
            m = re.search(r"subarray\((\d+),(\d+)\)", expr)
            self.assertIsNotNone(m, expr[:80])
            return base64.b64encode(payload[int(m.group(1)):int(m.group(2))]).decode()

        fake = FakeCDP(responder)
        self.assertEqual(fake.fetch_bytes("s", "/api/file", chunk=64), payload)
        self.assertGreater(len(calls), 3, "payload must be fetched in several chunks")
        self.assertIn("__buf = null", calls[-1], "page buffer must be released")

    def test_fetch_bytes_rejects_http_error(self):
        bad = FakeCDP(lambda e: "HTTP404")
        with self.assertRaises(RuntimeError):
            bad.fetch_bytes("s", "/api/file")

    def test_fetch_bytes_empty_body(self):
        fake = FakeCDP(lambda e: "OK:0" if "arrayBuffer" in e else None)
        self.assertEqual(fake.fetch_bytes("s", "/api/file"), b"")


class AttachmentPlanTests(unittest.TestCase):
    def test_collisions_get_unique_names(self):
        entries = {
            "aaaaaaaa-1": {"file_name": "frame_01.png", "file_kind": "image"},
            "bbbbbbbb-2": {"file_name": "frame_01.png", "file_kind": "image"},
            "cccccccc-3": {"file_name": "frame_01.png", "file_kind": "image"},
        }
        rows = attachments.plan(entries)
        names = [n for _u, _k, n in rows]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("frame_01.png", names)
        self.assertTrue(all(n.endswith(".png") for n in names))

    def test_entry_without_url_is_reported_not_dropped(self):
        conv = {"uuid": "c", "name": "c", "chat_messages": [{"files": [{
            "file_kind": "image", "file_uuid": "no-url-1", "file_name": "x.png",
            "size_bytes": 10,
        }]}]}

        class Fake:
            def new_page(self, url="about:blank"):
                return "t", "s"

            def close_page(self, tid):
                pass

            def navigate(self, *a, **k):
                pass

            def fetch_bytes(self, *a, **k):
                raise AssertionError("must not be called without a url")

        with tempfile.TemporaryDirectory() as td:
            convs = Path(td) / "raw/conversations_full"
            convs.mkdir(parents=True)
            (convs / "c.json").write_text(json.dumps(conv), encoding="utf-8")
            attachments.download(Fake(), "o", convs, Path(td) / "export/files", pace=0)
            missing = json.loads((Path(td) / "export/files_missing.json").read_text())
        self.assertEqual([m["file_uuid"] for m in missing], ["no-url-1"])

    def test_missing_extension_gets_kind_default(self):
        rows = attachments.plan({"u1": {"file_name": "screenshot", "file_kind": "image"}})
        self.assertEqual(rows[0][2], "screenshot.webp")


class ArtifactIdTests(unittest.TestCase):
    def test_collect_ids_dedupes_across_blocks(self):
        conv = {
            "uuid": "c1",
            "name": "t",
            "chat_messages": [
                {
                    "content": [
                        {"type": "tool_use", "name": "Artifact",
                         "input": {"action": "open", "url": "https://claude.ai/artifact/abc1234567"}},
                        {"type": "tool_result",
                         "content": [{"type": "text",
                                      "text": "Opened https://claude.ai/artifact/abc1234567 and /code/artifact/zzz9999999"}]},
                    ]
                }
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            Path(td, "c1.json").write_text(json.dumps(conv), encoding="utf-8")
            ids = artifacts.collect_ids(Path(td))
        self.assertEqual(sorted(ids), ["abc1234567", "zzz9999999"])


class RenderTests(unittest.TestCase):
    def test_tool_calls_and_attachments_survive(self):
        conv = {
            "uuid": "u", "name": "Demo", "model": "m",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "chat_messages": [
                {"sender": "human", "created_at": "2026-01-01T00:00:00Z",
                 "content": [{"type": "text", "text": "hi"}],
                 "files": [{"file_name": "a.png", "file_kind": "image", "size_bytes": 10}]},
                {"sender": "assistant", "created_at": "2026-01-01T00:01:00Z",
                 "content": [
                     {"type": "thinking", "thinking": "pondering"},
                     {"type": "tool_use", "name": "create_file", "input": {"path": "x.py"}},
                     {"type": "text", "text": "done"},
                 ]},
            ],
        }
        md = render.render_conversation(conv)
        for needle in ("# Demo", "👤 User", "🤖 Assistant", "thinking", "create_file", "a.png", "done"):
            self.assertIn(needle, md)

    def test_render_all_sorts_by_created_at(self):
        with tempfile.TemporaryDirectory() as td:
            convs = Path(td) / "raw/conversations_full"
            convs.mkdir(parents=True)
            for i, ts in enumerate(("2026-03-01T00:00:00Z", "2026-01-01T00:00:00Z")):
                (convs / f"c{i}.json").write_text(json.dumps({
                    "uuid": f"c{i}", "name": f"n{i}", "created_at": ts, "chat_messages": []
                }), encoding="utf-8")
            out = Path(td) / "export/markdown"
            idx = render.render_all(convs, out)
            self.assertEqual([r["name"] for r in idx], ["n1", "n0"])


class IndexTests(unittest.TestCase):
    def test_build_reads_all_sections(self):
        with tempfile.TemporaryDirectory() as td:
            export = Path(td) / "export"
            (export / "markdown").mkdir(parents=True)
            (export / "markdown/01_x.md").write_text("# x", encoding="utf-8")
            (export / "conversations_index.json").write_text(json.dumps([{
                "n": 1, "name": "x", "created": "2026-01-01 00:00", "messages": 2,
                "md": str(export / "markdown/01_x.md"),
            }]), encoding="utf-8")
            (export / "files_index.json").write_text(json.dumps([{
                "kind": "image", "name": "a.png", "size": 1024, "path": str(export / "a.png"), "conv": "x",
            }]), encoding="utf-8")
            (export / "files_missing.json").write_text(json.dumps([{
                "kind": "blob", "name": "b.tar.gz", "declared_bytes": 2048, "conv": "x",
            }]), encoding="utf-8")
            (export / "artifacts_manifest.json").write_text("[]", encoding="utf-8")
            live = export / "artifacts_live/abc"
            live.mkdir(parents=True)
            (live / "meta.json").write_text(json.dumps({"title": "T", "chars": 500}), encoding="utf-8")
            (live / "content.pdf").write_bytes(b"%PDF-1.4\n")
            (live / "content.txt").write_text("x" * 500, encoding="utf-8")

            dest = index.build(export, "org-1")
            text = dest.read_text(encoding="utf-8")
        self.assertIn("## 对话", text)
        self.assertIn("## 独立 Artifact", text)
        self.assertIn("## 附件（1 个）", text)
        self.assertIn("## 未能获取（1 个）", text)
        self.assertIn("org-1", text)


class SessionTests(unittest.TestCase):
    def test_default_profile_dir_is_per_port(self):
        a = session.Session(port=9301, quiet=True)
        b = session.Session(port=9302, quiet=True)
        self.assertNotEqual(a.profile_copy, b.profile_copy)
        self.assertIn("9301", str(a.profile_copy))

    def test_teardown_removes_profile_copy(self):
        with tempfile.TemporaryDirectory() as td:
            prof = Path(td) / "prof"
            prof.mkdir()
            (prof / "Cookies").write_text("x", encoding="utf-8")
            s = session.Session(profile_copy=prof, port=1, quiet=True)
            s._teardown()
            self.assertFalse(prof.exists())

    def test_keep_profile_is_respected(self):
        with tempfile.TemporaryDirectory() as td:
            prof = Path(td) / "prof"
            prof.mkdir()
            s = session.Session(profile_copy=prof, port=1, quiet=True, keep_profile=True)
            s._teardown()
            self.assertTrue(prof.exists())

    def test_enter_failure_does_not_leak_profile(self):
        """A 40MB profile copy must not survive a failed login."""
        with tempfile.TemporaryDirectory() as td:
            prof = Path(td) / "prof"

            def boom(_dest, _src=None, quiet=False):
                prof.mkdir(parents=True, exist_ok=True)
                (prof / "Cookies").write_text("x", encoding="utf-8")
                raise RuntimeError("not authenticated")

            original = session.copy_profile
            session.copy_profile = boom
            try:
                with self.assertRaises(RuntimeError):
                    with session.Session(profile_copy=prof, port=1, quiet=True):
                        pass
            finally:
                session.copy_profile = original
            self.assertFalse(prof.exists())

    def test_cookie_names_never_calls_keychain(self):
        import sqlite3

        def forbidden(*a, **k):  # any subprocess call would mean touching the Keychain
            raise AssertionError("subprocess must not be used")

        with tempfile.TemporaryDirectory() as td:
            prof = Path(td) / "prof"
            (prof / "Default").mkdir(parents=True)
            con = sqlite3.connect(prof / "Default/Cookies")
            con.execute("create table cookies (host_key text, name text, encrypted_value blob)")
            con.executemany("insert into cookies values (?,?,?)", [
                (".claude.ai", "sessionKey", b"v10garbage"),
                (".claude.ai", "cf_clearance", b"v10garbage"),
                ("example.com", "x", b"v10garbage"),
            ])
            con.commit()
            con.close()

            original = session.subprocess.run
            session.subprocess.run = forbidden
            try:
                names = session.cookie_names(prof)
            finally:
                session.subprocess.run = original
            self.assertEqual(names, ["cf_clearance", "sessionKey"])


class SessionExpiryTests(unittest.TestCase):
    class _Fake:
        def __init__(self, status=403):
            self.status = status
            self.calls = 0

        def fetch_json(self, session, path, headers=None):
            self.calls += 1
            return self.status, ({"error": "unauthorized"} if self.status != 200 else {"uuid": "u"})

        def new_page(self, url="about:blank"):
            return "t", "s"

        def close_page(self, target_id):
            pass

        def navigate(self, session, url, settle=0.0):
            pass

    def test_403_raises_session_expired(self):
        with self.assertRaises(conversations.SessionExpired):
            conversations.fetch_conversation(self._Fake(), "org", "uuid", "s")

    def test_download_all_does_not_retry_expired_session(self):
        fake = self._Fake()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(conversations.SessionExpired):
                conversations.download_all(fake, "org", Path(td) / "convs", pace=0)
        self.assertEqual(fake.calls, 1)  # 1 list call, no retry storm


class PathSafetyTests(unittest.TestCase):
    def test_safe_relative_cannot_escape(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "conv"
            base.mkdir()
            for evil in ("../../etc/passwd", "/etc/passwd", "a/../../b.txt", "./../../x", "..\\..\\win.ini"):
                got = util.safe_relative(base, evil)
                self.assertTrue(got.resolve().is_relative_to(base.resolve()), f"{evil} -> {got}")

    def test_extract_blocks_traversal_from_tool_input(self):
        conv = {
            "uuid": "deadbeef0000", "name": "pwn",
            "chat_messages": [{"content": [{
                "type": "tool_use", "name": "create_file",
                "input": {"path": "../../../escaped.txt", "file_text": "owned"},
            }]}],
        }
        with tempfile.TemporaryDirectory() as td:
            convs = Path(td) / "raw/conversations_full"
            convs.mkdir(parents=True)
            (convs / "c.json").write_text(json.dumps(conv), encoding="utf-8")
            out = Path(td) / "export/artifacts"
            artifacts.extract_from_conversations(convs, out)
            self.assertFalse((Path(td) / "escaped.txt").exists())
            self.assertFalse((Path(td) / "export/escaped.txt").exists())
            written = list(out.rglob("*.txt"))
            self.assertEqual(len(written), 1)
            self.assertTrue(str(written[0]).startswith(str(out)))


class AtomicWriteTests(unittest.TestCase):
    def test_failed_write_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "x.json"
            original = util.os.replace

            def boom(*a, **k):
                raise OSError("disk full")

            util.os.replace = boom
            try:
                with self.assertRaises(OSError):
                    util.write_atomic(target, "data")
            finally:
                util.os.replace = original
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(td).glob("*.part")), [])

    def test_overwrites_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "x.json"
            util.write_atomic(target, "one")
            util.write_atomic(target, "two")
            self.assertEqual(target.read_text(), "two")
            self.assertEqual(list(Path(td).glob("*.part")), [])


class ProfileSafetyTests(unittest.TestCase):
    def test_refuses_to_copy_onto_itself(self):
        with tempfile.TemporaryDirectory() as td:
            prof = Path(td) / "Real Edge"
            prof.mkdir()
            (prof / "keep.txt").write_text("precious", encoding="utf-8")
            with self.assertRaises(ValueError):
                session.copy_profile(dest=prof, source=prof, quiet=True)
            self.assertTrue((prof / "keep.txt").exists(), "real profile was deleted")

    def test_copy_strips_tab_restore_state(self):
        """The copy must not resurrect the user's open tabs (they may be unrelated sites)."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "real"
            (src / "Default/Sessions").mkdir(parents=True)
            (src / "Default/Sessions/Session_1").write_text("x")
            (src / "Default/Last Tabs").write_text("x")
            (src / "Default/Preferences").write_text(
                json.dumps({"profile": {"restore_on_startup": 1, "exit_type": "Crashed"}}),
                encoding="utf-8",
            )
            dst = Path(td) / "copy"
            session.copy_profile(dest=dst, source=src, quiet=True)
            self.assertFalse((dst / "Default/Sessions").exists())
            self.assertFalse((dst / "Default/Last Tabs").exists())
            prefs = json.loads((dst / "Default/Preferences").read_text(encoding="utf-8"))
            self.assertEqual(prefs["profile"]["restore_on_startup"], 5)
            self.assertNotIn("exit_type", prefs["profile"])

    def test_copy_strips_singleton_links(self):
        """Leftover Singleton sockets make the copy hand off to the live Edge."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "real"
            (src / "Default").mkdir(parents=True)
            (src / "Default/Cookies").write_text("", encoding="utf-8")
            for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                (src / name).symlink_to("/tmp/somewhere-else")
            dst = Path(td) / "copy"
            session.copy_profile(dest=dst, source=src, quiet=True)
            self.assertEqual([p.name for p in dst.glob("Singleton*")], [])

    def test_refuses_dest_inside_source(self):
        with tempfile.TemporaryDirectory() as td:
            prof = Path(td) / "Real"
            prof.mkdir()
            with self.assertRaises(ValueError):
                session.copy_profile(dest=prof / "copy", source=prof, quiet=True)


class ProxyBypassTests(unittest.TestCase):
    def test_devtools_discovery_ignores_http_proxy(self):
        import http.server
        import os
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # silence
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")}
        os.environ.update({"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"})
        try:
            self.assertEqual(cdp.browser_ws(port, timeout=3), "ws://127.0.0.1:1/devtools/browser/x")
        finally:
            srv.shutdown()
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class OutputPathTests(unittest.TestCase):
    def test_root_out_is_rejected(self):
        with self.assertRaises(ValueError):
            cli._paths(Path("/"))

    def test_index_skips_artifact_without_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            export = Path(td) / "export"
            live = export / "artifacts_live/abc"
            live.mkdir(parents=True)
            (live / "meta.json").write_text(json.dumps({"title": "ghost", "chars": 9}), encoding="utf-8")
            index.build(export, None)
            text = (export / "README.md").read_text(encoding="utf-8")
            self.assertNotIn("ghost", text)


class LinkResolutionTests(unittest.TestCase):
    def _build(self, export: Path, path_value: str):
        f = export / "files/a.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        (export / "files_index.json").write_text(json.dumps([{
            "kind": "image", "name": "a.png", "size": 1, "path": path_value, "conv": "c",
        }]), encoding="utf-8")
        index.build(export, None)
        return (export / "README.md").read_text(encoding="utf-8")

    def test_legacy_repo_relative_path(self):
        """Older exports recorded 'export/files/...' - the link must not double the prefix."""
        import re
        with tempfile.TemporaryDirectory() as td:
            export = Path(td) / "export"
            export.mkdir()
            text = self._build(export, "export/files/a.png")
            links = [l for l in re.findall(r"\]\(([^)]+)\)", text) if l.endswith("a.png")]
            self.assertTrue(links and links[0] == "files/a.png", links)

    def test_filenames_with_spaces_and_parens_are_encoded(self):
        import re
        from urllib.parse import unquote
        with tempfile.TemporaryDirectory() as td:
            export = Path(td) / "export"
            export.mkdir()
            name = "arrays in c (1).pdf"
            (export / "files/document").mkdir(parents=True)
            (export / "files/document" / name).write_bytes(b"x")
            (export / "files_index.json").write_text(json.dumps([{
                "kind": "document", "name": name, "size": 1,
                "path": f"files/document/{name}", "conv": "c",
            }]), encoding="utf-8")
            index.build(export, None)
            text = (export / "README.md").read_text(encoding="utf-8")
            links = [unquote(l) for l in re.findall(r"\]\(([^)]+)\)", text)]
            self.assertIn(f"files/document/{name}", links)

    def test_absolute_path_from_another_machine(self):
        import re
        with tempfile.TemporaryDirectory() as td:
            export = Path(td) / "export"
            export.mkdir()
            (export / "files").mkdir(exist_ok=True)
            (export / "files/a.png").write_bytes(b"x")
            text = self._build(export, str(export / "files/a.png"))
            links = [l for l in re.findall(r"\]\(([^)]+)\)", text) if l.endswith("a.png")]
            self.assertTrue(links and links[0] == "files/a.png", links)


class LimitKeepsIndexTests(unittest.TestCase):
    class _Fake:
        def new_page(self, url="about:blank"):
            return "t", "s"

        def close_page(self, target_id):
            pass

        def navigate(self, session, url, settle=0.0):
            pass

        def fetch_bytes(self, session, url, chunk=None):
            return b"payload"

    def _conv_with_files(self, n: int) -> dict:
        files = [{
            "file_kind": "image", "file_uuid": f"uuid-{i}", "file_name": f"pic{i}.png",
            "size_bytes": 7, "preview_url": f"/api/o/files/uuid-{i}/preview",
        } for i in range(n)]
        return {"uuid": "c", "name": "c", "chat_messages": [{"files": files}]}

    def test_limit_does_not_shrink_files_index(self):
        with tempfile.TemporaryDirectory() as td:
            convs = Path(td) / "raw/conversations_full"
            convs.mkdir(parents=True)
            (convs / "c.json").write_text(json.dumps(self._conv_with_files(3)), encoding="utf-8")
            out = Path(td) / "export/files"
            res = attachments.download(self._Fake(), "o", convs, out, pace=0, limit=1)
            index = json.loads((Path(td) / "export/files_index.json").read_text())
            self.assertEqual(res["ok"], 1, "only one download attempted")
            self.assertEqual(len(index), 1, "index reflects what is on disk")
            self.assertEqual(len(list((out / "image").glob("*"))), 1)
            missing = json.loads((Path(td) / "export/files_missing.json").read_text())
            self.assertEqual(missing, [], "untouched files are not 'missing'")

    def test_limit_does_not_erase_index_of_existing_files(self):
        """Regression: a limited run used to rebuild the index from the truncated
        row list, wiping the record of files already on disk."""
        with tempfile.TemporaryDirectory() as td:
            convs = Path(td) / "raw/conversations_full"
            convs.mkdir(parents=True)
            (convs / "c.json").write_text(json.dumps(self._conv_with_files(3)), encoding="utf-8")
            out = Path(td) / "export/files"
            attachments.download(self._Fake(), "o", convs, out, pace=0)          # full run
            before = json.loads((Path(td) / "export/files_index.json").read_text())
            self.assertEqual(len(before), 3)

            attachments.download(self._Fake(), "o", convs, out, pace=0, limit=1)  # limited run
            after = json.loads((Path(td) / "export/files_index.json").read_text())
            self.assertEqual(len(after), 3, "limited run must not shrink the index")

    def test_full_run_indexes_everything(self):
        with tempfile.TemporaryDirectory() as td:
            convs = Path(td) / "raw/conversations_full"
            convs.mkdir(parents=True)
            (convs / "c.json").write_text(json.dumps(self._conv_with_files(3)), encoding="utf-8")
            out = Path(td) / "export/files"
            attachments.download(self._Fake(), "o", convs, out, pace=0)
            index = json.loads((Path(td) / "export/files_index.json").read_text())
            self.assertEqual(len(index), 3)


class AttachmentNameTests(unittest.TestCase):
    def test_dot_dot_and_slashes_are_neutralised(self):
        for evil in ("..", ".", "../../etc/passwd", "a/b.png", "", ".hidden"):
            safe = attachments._safe_name(evil, "uuid-1234")
            self.assertNotIn("/", safe)
            self.assertNotIn(safe, (".", ".."))
            self.assertTrue(safe)

    def test_plan_never_escapes(self):
        rows = attachments.plan({"u1": {"file_name": "../../x.png", "file_kind": "image"}})
        self.assertNotIn("/", rows[0][2])
        self.assertNotIn("..", rows[0][2])


class ContextLossTests(unittest.TestCase):
    def test_markers_detected(self):
        for msg in ("Cannot find context with specified id",
                    "Execution context was destroyed.",
                    "Session with given id not found."):
            self.assertTrue(cdp.is_context_lost(RuntimeError(msg)), msg)
        self.assertFalse(cdp.is_context_lost(RuntimeError("HTTP 500")))


class PortGuardTests(unittest.TestCase):
    def test_busy_port_fails_before_copying_profile(self):
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        # backlog must exceed the number of probe connects, otherwise the kernel
        # refuses the later ones and port_in_use() would report False
        srv.listen(16)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(session.port_in_use(port))
            with tempfile.TemporaryDirectory() as td:
                prof = Path(td) / "copy"
                with self.assertRaises(RuntimeError) as ctx:
                    with session.Session(port=port, profile_copy=prof, quiet=True):
                        pass
                self.assertIn("already in use", str(ctx.exception))
                self.assertFalse(prof.exists(), "profile copied before the port check")
        finally:
            srv.close()


class CorruptFileTests(unittest.TestCase):
    def test_unreadable_conversation_warns_but_continues(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "good.json").write_text(json.dumps({"uuid": "g"}), encoding="utf-8")
            (d / "bad.json").write_text("{not json", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rows = conversations.load_local(d)
            self.assertEqual(len(rows), 1)
            self.assertIn("unreadable", err.getvalue())


class StabilizationCaptureTests(unittest.TestCase):
    """The viewer renders progressively; capture must keep the richest snapshot."""

    class _Fake:
        def __init__(self, seq):
            self.seq = list(seq)
            self.prints = 0

        def new_page(self, url="about:blank"):
            return "t", "s"

        def close_page(self, tid):
            pass

        def navigate(self, session, url, settle=0.0):
            pass

        def eval(self, session, expr, await_promise=False):
            if "location.pathname" in expr:
                return "/artifact/x"
            if "document.title" in expr:
                return "T"
            return None

        def print_pdf(self, session, **kw):
            self.prints += 1
            n = self.seq.pop(0) if self.seq else 0
            return b"P" * n  # pdf size encodes how much content rendered

    def _patched(self, extra):
        clock = FakeClock()
        stack = list(clock.patches()) + extra
        for p in stack:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])

    def _run(self, seq, force=True, retries=5):
        from unittest.mock import patch
        fake = self._Fake(seq)

        def fake_text(pdf_path):
            return "x" * len(Path(pdf_path).read_bytes())

        self._patched([patch.object(artifacts, "pdf_text", fake_text)])
        with tempfile.TemporaryDirectory() as td:
            artifacts.capture(fake, ["abc123"], Path(td), pace=0, min_chars=100,
                              retries=retries, force=force)
            txt = (Path(td) / "abc123/content.txt").read_text()
        return fake.prints, len(txt)

    def test_stops_when_text_stabilises(self):
        prints, chars = self._run([0, 500, 500, 900])
        self.assertEqual(prints, 3, "should stop at the first stable pair")
        self.assertEqual(chars, 500)

    def test_keeps_growing_capture(self):
        _, chars = self._run([0, 120, 340, 340])
        self.assertEqual(chars, 340)

    def test_respects_retry_budget(self):
        prints, chars = self._run([10, 20, 30, 40, 50])
        self.assertEqual(prints, 5, "retries caps the number of captures")
        self.assertEqual(chars, 50)

    def test_force_retries_markdown_but_reuses_complete_pdf(self):
        """--force means "try markdown again", not "burn 40s re-rendering a good PDF"."""
        from unittest.mock import patch
        self._patched([patch.object(artifacts, "pdf_text",
                                    lambda p: "x" * len(Path(p).read_bytes()))])
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "abc123"
            d.mkdir(parents=True)
            (d / "content.pdf").write_bytes(b"P" * 400)
            (d / "content.txt").write_text("x" * 400, encoding="utf-8")
            fake = self._Fake([0])
            artifacts.capture(fake, ["abc123"], Path(td), pace=0, retries=1, force=True)
            self.assertEqual(fake.prints, 0, "complete PDF should be reused")

    def test_pdf_only_forces_a_render(self):
        from unittest.mock import patch
        self._patched([patch.object(artifacts, "pdf_text",
                                    lambda p: "x" * len(Path(p).read_bytes()))])
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "abc123"
            d.mkdir(parents=True)
            (d / "content.txt").write_text("x" * 400, encoding="utf-8")
            fake = self._Fake([400])
            artifacts.capture(fake, ["abc123"], Path(td), pace=0, retries=1,
                              force=True, pdf_only=True)
            self.assertEqual(fake.prints, 1)

    def test_force_false_skips_complete(self):
        from unittest.mock import patch
        self._patched([patch.object(artifacts, "pdf_text",
                                    lambda p: "x" * len(Path(p).read_bytes()))])
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "abc123"
            d.mkdir(parents=True)
            (d / "content.pdf").write_bytes(b"P" * 400)
            (d / "content.txt").write_text("x" * 400, encoding="utf-8")
            fake = self._Fake([0])
            artifacts.capture(fake, ["abc123"], Path(td), pace=0, min_chars=100)
            self.assertEqual(fake.prints, 0, "complete output should be skipped")


class MarkdownExportTests(unittest.TestCase):
    class _Fake:
        def __init__(self, dl: Path, has_markdown=True, payload=b"# hi\n\nbody\n"):
            self.dl = dl
            self.has_markdown = has_markdown
            self.payload = payload
            self.created = None

        def send(self, method, params=None, session=None):
            # export_markdown redirects downloads into a per-artifact subdirectory
            if method == "Browser.setDownloadBehavior":
                self.dl = Path(params["downloadPath"])
                self.dl.mkdir(parents=True, exist_ok=True)
            return {}

        def new_page(self, url="about:blank"):
            return "t", "s"

        def close_page(self, tid):
            pass

        def navigate(self, session, url, settle=0.0):
            pass

        def eval(self, session, expr, await_promise=False):
            if "querySelectorAll('button')" in expr and "Export" in expr and "!!" in expr:
                return True
            if "^Markdown" in expr:
                if not self.has_markdown:
                    return 0
                self.created = self.dl / "Some Artifact.md"
                self.created.write_bytes(self.payload)
                return 1
            if "dialog" in expr:
                return ""
            return 1

    def test_download_is_moved_into_place(self):
        with tempfile.TemporaryDirectory() as td:
            dl = Path(td) / "dl"
            dest = Path(td) / "out/abc/content.md"
            fake = self._Fake(dl)
            clock = FakeClock()
            for patcher in clock.patches():
                patcher.start()
                self.addCleanup(patcher.stop)
            got = artifacts.export_markdown(fake, "abc", dest, dl)
            self.assertIsNotNone(got)
            self.assertEqual(dest.read_text(), "# hi\n\nbody\n")
            self.assertFalse(fake.created.exists(), "source download must be moved, not copied")

    def test_returns_none_when_no_markdown_option(self):
        with tempfile.TemporaryDirectory() as td:
            dl = Path(td) / "dl"
            dest = Path(td) / "out/abc/content.md"
            clock = FakeClock()
            for patcher in clock.patches():
                patcher.start()
                self.addCleanup(patcher.stop)
            got = artifacts.export_markdown(self._Fake(dl, has_markdown=False), "abc", dest, dl)
            self.assertIsNone(got)
            self.assertFalse(dest.exists())


class NewModuleSafetyTests(unittest.TestCase):
    """Regressions for the markdown-export rewrite."""

    def test_escape_never_cancels_a_pending_save(self):
        class Fake:
            def __init__(self):
                self.keys = []

            def eval(self, session, expr, await_promise=False):
                return "Save this file?\nArtifact.md\n30.7 kB\nCancel\nSave file"

            def send(self, method, params=None, session=None):
                self.keys.append(method)
                return {}

        fake = Fake()
        text = artifacts._dismiss_dialog(fake, "s")
        self.assertIn("Save", text)
        self.assertEqual(fake.keys, [], "must not press Escape on a save prompt")
        first = artifacts._dismiss_dialog.__doc__
        self.assertIsNotNone(first)

    def test_escape_still_closes_the_sharing_sheet(self):
        class Fake:
            def __init__(self):
                self.keys = []
                self.calls = 0

            def eval(self, session, expr, await_promise=False):
                self.calls += 1
                return "Sharing isn't included in your plan" if self.calls == 1 else ""

            def send(self, method, params=None, session=None):
                self.keys.append(method)
                return {}

        fake = Fake()
        artifacts._dismiss_dialog(fake, "s")
        self.assertIn("Input.dispatchKeyEvent", fake.keys)

    def test_downloads_are_isolated_per_artifact(self):
        """A late download from artifact A must not be attributed to artifact B."""
        with tempfile.TemporaryDirectory() as td:
            dl = Path(td) / "dl"
            seen = []

            class Fake:
                def __init__(self):
                    self.path = None
                    self.own = None

                def send(self, method, params=None, session=None):
                    if method == "Browser.setDownloadBehavior":
                        self.path = Path(params["downloadPath"])
                        seen.append(self.path)
                    return {}

                def new_page(self, url="about:blank"):
                    return "t", "s"

                def close_page(self, tid):
                    pass

                def navigate(self, session, url, settle=0.0):
                    pass

                def eval(self, session, expr, await_promise=False):
                    if "querySelectorAll('button')" in expr and "!!" in expr:
                        return True
                    if "^Markdown" in expr:
                        (self.path / "out.md").write_text("# real", encoding="utf-8")
                        return 1
                    if "dialog" in expr:
                        return ""
                    return 1

                def print_pdf(self, *a, **k):
                    return b""

            clock = FakeClock()
            for patcher in clock.patches():
                patcher.start()
                self.addCleanup(patcher.stop)

            a = Fake()
            artifacts.export_markdown(a, "artifactA", Path(td) / "A.md", dl)
            # a stale file appears in the shared parent directory afterwards
            (dl / "stale.md").write_text("# stale", encoding="utf-8")
            b = Fake()
            got = artifacts.export_markdown(b, "artifactB", Path(td) / "B.md", dl)
            self.assertIsNotNone(got)
            self.assertEqual((Path(td) / "B.md").read_text(), "# real")
            self.assertNotEqual(seen[0], seen[1], "each artifact needs its own download dir")
            self.assertFalse((Path(td) / "B.md").read_text().startswith("# stale"))

    def test_capture_cleans_temp_dir_on_error(self):
        from unittest.mock import patch

        class Fake:
            def send(self, *a, **k):
                return {}

            def new_page(self, url="about:blank"):
                return "t", "s"

            def close_page(self, tid):
                pass

            def navigate(self, *a, **k):
                pass

            def eval(self, *a, **k):
                return "/artifact/x"

            def print_pdf(self, *a, **k):
                return b""

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "dl"
            tmp.mkdir()
            clock = FakeClock()
            patchers = clock.patches() + [
                patch.object(artifacts.tempfile, "mkdtemp", lambda **k: str(tmp)),
                patch.object(artifacts, "export_markdown",
                             lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))),
            ]
            for patcher in patchers:
                patcher.start()
                self.addCleanup(patcher.stop)
            results = artifacts.capture(Fake(), ["abc"], Path(td) / "out", pace=0,
                                        retries=1, force=True)
            self.assertTrue(results and "error" in results[0])
            self.assertFalse(tmp.exists(), "temp download dir must be removed")

    def test_pdf_attempt_files_are_cleaned_up(self):
        from unittest.mock import patch

        class Fake:
            def print_pdf(self, session, **kw):
                return b"P" * 10

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)
            calls = {"n": 0}

            def boom(_p):
                calls["n"] += 1
                raise RuntimeError("pdftotext failed")

            clock = FakeClock()
            patchers = clock.patches() + [patch.object(artifacts, "pdf_text", boom)]
            for patcher in patchers:
                patcher.start()
                self.addCleanup(patcher.stop)
            with self.assertRaises(RuntimeError):
                artifacts._capture_pdf(Fake(), "s", dest, retries=1, min_chars=10)
            self.assertEqual(list(dest.glob(".try*.pdf")), [], "no leftover attempt files")


class IdempotencyTests(unittest.TestCase):
    """Running a step twice must not duplicate or mangle output."""

    def _conv(self):
        return {"uuid": "aaaabbbbcccc", "name": "dup", "chat_messages": [{"content": [
            {"type": "tool_use", "name": "create_file",
             "input": {"path": "out/a.py", "file_text": "print(1)\n"}},
        ]}]}

    def _run(self, convs: Path, out: Path) -> int:
        artifacts.extract_from_conversations(convs, out)
        return len(list(out.rglob("*.*")))

    def test_second_run_overwrites_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as td:
            convs = Path(td) / "raw/conversations_full"
            convs.mkdir(parents=True)
            (convs / "c.json").write_text(json.dumps(self._conv()), encoding="utf-8")
            out = Path(td) / "export/artifacts"
            first = self._run(convs, out)
            second = self._run(convs, out)
            third = self._run(convs, out)
            self.assertEqual(first, second, "re-running must not add files")
            self.assertEqual(second, third)
            self.assertEqual(len(list((out).rglob("a.py"))), 1)

    def test_same_path_different_content_gets_a_suffix(self):
        conv = {"uuid": "aaaabbbbcccc", "name": "dup", "chat_messages": [
            {"content": [{"type": "tool_use", "name": "create_file",
                          "input": {"path": "out/b.py", "file_text": "v1"}}]},
            {"content": [{"type": "tool_use", "name": "create_file",
                          "input": {"path": "out/b.py", "file_text": "v2"}}]},
        ]}
        with tempfile.TemporaryDirectory() as td:
            convs = Path(td) / "raw/conversations_full"
            convs.mkdir(parents=True)
            (convs / "c.json").write_text(json.dumps(conv), encoding="utf-8")
            out = Path(td) / "export/artifacts"
            artifacts.extract_from_conversations(convs, out)
            files = sorted(p.name for p in out.rglob("b*.py"))
            self.assertEqual(files, ["b.py", "b_1.py"], "distinct payloads must not overwrite")
            self.assertEqual({(out.rglob("b.py")).__next__().read_text(),
                              (out.rglob("b_1.py")).__next__().read_text()}, {"v1", "v2"})


class LocalImportTests(unittest.TestCase):
    def test_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "dl"
            src.mkdir()
            (src / "A.md").write_text("# A\n\nbody", encoding="utf-8")
            export = Path(td) / "export"
            first = local_import.import_dir(export, src)
            second = local_import.import_dir(export, src)
            self.assertEqual(first["imported"], 1)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["skipped"], 1)
            self.assertEqual(len(local_import.load_index(export)), 1)

    def test_duplicate_of_api_artifact_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            export = Path(td) / "export"
            live = export / "artifacts_live" / "abc123"
            live.mkdir(parents=True)
            (live / "content.md").write_text("# same", encoding="utf-8")
            src = Path(td) / "dl"
            src.mkdir()
            (src / "copy.md").write_text("# same", encoding="utf-8")
            local_import.import_dir(export, src)
            rows = local_import.load_index(export)
            self.assertEqual(rows[0]["duplicate_of"], "abc123")

    def test_move_removes_source_only_after_import(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "dl"
            src.mkdir()
            f = src / "A.md"
            f.write_text("# A", encoding="utf-8")
            export = Path(td) / "export"
            local_import.import_dir(export, src, move=True)
            self.assertFalse(f.exists())
            self.assertEqual(len(list((export / "artifacts_local").rglob("content.md"))), 1)

    def test_similarity_is_recorded_for_older_revision(self):
        with tempfile.TemporaryDirectory() as td:
            export = Path(td) / "export"
            live = export / "artifacts_live" / "api1"
            live.mkdir(parents=True)
            body = "\n".join(f"line number {i} with enough text" for i in range(12))
            (live / "content.md").write_text("# T\n\n" + body, encoding="utf-8")
            src = Path(td) / "dl"
            src.mkdir()
            # local copy is a strict subset -> older / less complete revision
            subset = "\n".join(f"line number {i} with enough text" for i in range(6))
            (src / "older.md").write_text("# T\n\n" + subset, encoding="utf-8")
            local_import.import_dir(export, src)
            row = local_import.load_index(export)[0]
            self.assertEqual(row["similar_to"], "api1")
            self.assertGreaterEqual(row["overlap"], 0.9)
            self.assertIsNone(row["duplicate_of"])

    def test_refresh_recomputes_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            export = Path(td) / "export"
            src = Path(td) / "dl"
            src.mkdir()
            (src / "x.md").write_text("# X\n\n" + "\n".join(f"row {i} long enough" for i in range(5)),
                                      encoding="utf-8")
            local_import.import_dir(export, src)
            self.assertIsNone(local_import.load_index(export)[0]["similar_to"])
            # an API artifact appears later with the same lines
            live = export / "artifacts_live" / "api2"
            live.mkdir(parents=True)
            (live / "content.md").write_text(
                "# X\n\n" + "\n".join(f"row {i} long enough" for i in range(5)), encoding="utf-8")
            local_import.refresh(export)
            row = local_import.load_index(export)[0]
            self.assertEqual(row["similar_to"], "api2")
            self.assertEqual(row["overlap"], 1.0)

    def test_index_lists_local_imports(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "dl"
            src.mkdir()
            (src / "My Artifact.md").write_text("# mine\n\ntext", encoding="utf-8")
            export = Path(td) / "export"
            local_import.import_dir(export, src)
            text = index.build(export, None).read_text(encoding="utf-8")
            self.assertIn("本地导入的 Artifact", text)
            self.assertIn("My Artifact", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
