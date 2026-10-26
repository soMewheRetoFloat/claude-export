"""Command line interface.

    python -m claude_export doctor
    python -m claude_export conversations --out export
    python -m claude_export artifacts
    python -m claude_export attachments
    python -m claude_export render
    python -m claude_export index
    python -m claude_export all

Global options may be given before *or* after the subcommand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import artifacts, attachments, conversations, index, local_import, render, session

# Options valid for every subcommand. Registered on the main parser and again on
# each subparser with default=SUPPRESS, so either position works and the
# subparser never clobbers a value that was already given globally.
COMMON = [
    (("--out",), {"default": "export", "help": "output directory (default: export)"}),
    (("--org",), {"default": None, "help": "organization uuid (default: first one)"}),
    (("--port",), {"type": int, "default": 9222, "help": "devtools port (default: 9222)"}),
    (("--pace",), {"type": float, "default": 2.5, "help": "seconds between requests"}),
    (("--profile-copy",), {"default": None,
                            "help": "temp profile copy path (default: per-port, under /tmp)"}),
    (("--keep-profile",), {"action": "store_true", "help": "do not delete the profile copy on exit"}),
    (("--headless",), {"action": "store_true", "help": "run Edge headless (less reliable)"}),
    (("--quiet",), {"action": "store_true", "help": "less logging"}),
    (("--limit",), {"type": int, "default": None, "help": "only process N items"}),
]


def _paths(out: Path) -> dict[str, Path]:
    if out.parent == out:
        raise ValueError(f"--out {out} is a filesystem root; pick a subdirectory")
    raw = out.parent / "raw"
    return {
        "out": out,
        "raw": raw,
        "convs": raw / "conversations_full",
        "markdown": out / "markdown",
        "artifacts": out / "artifacts",
        "live": out / "artifacts_live",
        "files": out / "files",
    }


def _out(args) -> Path:
    return Path(args.out).expanduser().resolve()


def _session(args) -> session.Session:
    return session.Session(
        port=args.port,
        profile_copy=Path(args.profile_copy) if getattr(args, "profile_copy", None) else None,
        org=getattr(args, "org", None),
        keep_profile=getattr(args, "keep_profile", False),
        headless=getattr(args, "headless", False),
        quiet=getattr(args, "quiet", False),
    )


# ------------------------------------------------------------------- steps
def _run_conversations(cdp, org, p, args) -> None:
    conversations.download_all(cdp, org, p["convs"], pace=args.pace, limit=args.limit)


def _run_artifacts(cdp, org, p, args) -> None:
    artifacts.extract_from_conversations(p["convs"], p["artifacts"])
    ids = artifacts.collect_ids(p["convs"])
    print(f"standalone artifacts referenced: {len(ids)}")
    if not ids or getattr(args, "extract_only", False):
        return
    if args.limit:
        ids = ids[: args.limit]
    min_chars = getattr(args, "min_chars", 400)
    artifacts.capture(cdp, ids, p["live"], pace=args.pace, min_chars=min_chars,
                      force=getattr(args, "force", False),
                      pdf_only=getattr(args, "pdf_only", False))
    for aid, n in artifacts.verify(p["live"]):
        if n < min_chars:
            print(f"  weak: {aid} -> {n} chars")


def _run_attachments(cdp, org, p, args) -> None:
    attachments.download(cdp, org, p["convs"], p["files"], pace=args.pace, limit=args.limit)


# ---------------------------------------------------------------- commands
def cmd_doctor(args) -> int:
    p = _paths(_out(args))
    print("Edge profile :", session.PROFILE_DIR)
    print("exists       :", session.PROFILE_DIR.exists())
    try:
        names = session.cookie_names(session.PROFILE_DIR)
        print(f"cookie db    : {len(names)} names for claude.ai (sessionKey={'sessionKey' in names})")
        print("               (names only - no Keychain access, no decryption)")
    except Exception as e:  # noqa: BLE001
        print("cookie db    : FAILED —", e)
    with _session(args) as (cdp, org):
        print("auth         : OK, org =", org)
        try:
            print("conversations:", len(conversations.list_conversations(cdp, org)))
        except Exception as e:  # noqa: BLE001
            print("conversations: FAILED —", e)
    print("output dir   :", p["out"])
    return 0


def cmd_conversations(args) -> int:
    p = _paths(_out(args))
    with _session(args) as (cdp, org):
        _run_conversations(cdp, org, p, args)
    return 0


def cmd_artifacts(args) -> int:
    p = _paths(_out(args))
    if not p["convs"].exists():
        print("error: no conversations found - run `conversations` first", file=sys.stderr)
        return 1
    if getattr(args, "extract_only", False):
        # purely offline: rewrite the generated-file tree, no browser needed
        artifacts.extract_from_conversations(p["convs"], p["artifacts"])
        return 0
    with _session(args) as (cdp, org):
        _run_artifacts(cdp, org, p, args)
    return 0


def cmd_attachments(args) -> int:
    p = _paths(_out(args))
    if not p["convs"].exists():
        print("error: no conversations found - run `conversations` first", file=sys.stderr)
        return 1
    with _session(args) as (cdp, org):
        _run_attachments(cdp, org, p, args)
    return 0


def cmd_render(args) -> int:
    p = _paths(_out(args))
    if not p["convs"].exists():
        print("error: no conversations found - run `conversations` first", file=sys.stderr)
        return 1
    render.render_all(p["convs"], p["markdown"])
    return 0


def cmd_index(args) -> int:
    p = _paths(_out(args))
    index.build(p["out"], getattr(args, "org", None))
    return 0


def cmd_import_local(args) -> int:
    """File manually downloaded artifact Markdown into the export (offline)."""
    p = _paths(_out(args))
    src = Path(getattr(args, "from_dir", None) or "~/Downloads").expanduser()
    if not src.exists():
        print(f"error: source directory not found: {src}", file=sys.stderr)
        return 1
    if getattr(args, "refresh", False):
        local_import.refresh(p["out"])
        return 0
    print(f"importing from {src}")
    local_import.import_dir(
        p["out"], src,
        move=getattr(args, "move", False),
        pattern=getattr(args, "pattern", "*.md"),
        limit=args.limit,
    )
    return 0


def cmd_all(args) -> int:
    """One browser session for every network step, then the offline ones."""
    p = _paths(_out(args))
    steps = [
        ("conversations", _run_conversations),
        ("artifacts", _run_artifacts),
        ("attachments", _run_attachments),
    ]
    failed_steps: list[str] = []
    with _session(args) as (cdp, org):
        for name, fn in steps:
            try:
                fn(cdp, org, p, args)
            except Exception as e:  # noqa: BLE001
                failed_steps.append(name)
                print(f"\n[!] step '{name}' failed: {e}", file=sys.stderr)
                print("    continuing with the remaining steps", file=sys.stderr)

    # Do not manufacture an empty export when the very first step never produced
    # anything to render.
    if p["convs"].exists() and any(p["convs"].iterdir()):
        render.render_all(p["convs"], p["markdown"])
        index.build(p["out"], getattr(args, "org", None))
    else:
        print("no conversations on disk - skipping render/index", file=sys.stderr)
        return 1

    return 1 if failed_steps else 0


COMMANDS = [
    ("doctor", cmd_doctor, "check profile, cookies and login state", False),
    ("conversations", cmd_conversations, "download all conversations with tool calls", False),
    ("artifacts", cmd_artifacts, "extract generated files + capture standalone artifacts", True),
    ("attachments", cmd_attachments, "download attachments", False),
    ("render", cmd_render, "render conversations to Markdown", False),
    ("index", cmd_index, "build README index", False),
    ("import-local", cmd_import_local, "import artifact .md files you downloaded yourself", False),
    ("all", cmd_all, "everything (one session) + render + index", True),
]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m claude_export",
        description="Export your own claude.ai data through the logged-in Edge profile.",
    )
    for flags, kw in COMMON:
        ap.add_argument(*flags, **kw)

    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_, wants_artifact_flags in COMMANDS:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        for flags, kw in COMMON:
            sp.add_argument(*flags, **{**kw, "default": argparse.SUPPRESS})
        if name == "import-local":
            sp.add_argument("--from", dest="from_dir", default=None,
                            help="directory to import from (default: ~/Downloads)")
            sp.add_argument("--pattern", default="*.md", help="glob to import (default: *.md)")
            sp.add_argument("--move", action="store_true", default=argparse.SUPPRESS,
                            help="delete the source file after a successful import")
            sp.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS,
                            help="recompute duplicate/similarity metadata for existing imports")
        if wants_artifact_flags:
            sp.add_argument("--extract-only", action="store_true",
                            default=argparse.SUPPRESS,
                            help="skip standalone artifact capture")
            sp.add_argument("--min-chars", type=int, default=argparse.SUPPRESS,
                            help="min extracted text per artifact (default 400)")
            sp.add_argument("--force", action="store_true", default=argparse.SUPPRESS,
                            help="re-capture artifacts that already have output")
            sp.add_argument("--pdf-only", action="store_true", default=argparse.SUPPRESS,
                            help="skip the markdown export, always render a PDF")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for key, val in (("pace", 2.5), ("port", 9222), ("out", "export"), ("limit", None)):
        if not hasattr(args, key):  # pragma: no cover - defensive
            setattr(args, key, val)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
