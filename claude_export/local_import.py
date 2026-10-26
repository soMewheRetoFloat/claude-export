"""Import artifact Markdown that the API cannot reach.

Some artifacts never show up in a conversation or in the harvested id list:
ones you uploaded yourself, or that someone shared with you. You tend to already
have them as `.md` files in Downloads - this module files them into the export
next to the API-exported ones, without touching that tree.

Design notes:
  * keyed by content hash, so importing the same file twice is a no-op
  * an imported file that duplicates an API export is recorded as such and kept,
    because the local copy may be a different (older/newer) revision
  * `move=True` removes the source only after the import succeeded
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

from .util import write_atomic

INDEX_NAME = "artifacts_local.json"


def _slug(text: str, n: int = 48) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[^\w\u4e00-\u9fff\-.]+", "_", text)
    return (text or "artifact")[:n].strip("_") or "artifact"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_index(export: Path) -> list[dict]:
    try:
        return json.loads((export / INDEX_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def _lines(text: str) -> set[str]:
    return {re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()
            if len(ln.strip()) > 12}


def _api_profiles(export: Path) -> list[tuple[str, set[str]]]:
    """(artifact id, normalised lines) for everything the API exported."""
    out: list[tuple[str, set[str]]] = []
    live = export / "artifacts_live"
    if not live.exists():
        return out
    for d in sorted(live.glob("*")):
        md = d / "content.md"
        if md.exists():
            out.append((d.name, _lines(md.read_text(encoding="utf-8", errors="replace"))))
    return out


def _best_match(lines: set[str], profiles: list[tuple[str, set[str]]]) -> tuple[str | None, float]:
    """Containment score: how much of the local file already exists in an artifact."""
    if not lines:
        return None, 0.0
    best, score = None, 0.0
    for aid, other in profiles:
        if not other:
            continue
        ratio = len(lines & other) / len(lines)
        if ratio > score:
            best, score = aid, ratio
    return best, round(score, 3)


def _api_hashes(export: Path) -> dict[str, str]:
    """sha256 -> artifact id, for everything the API already exported."""
    seen: dict[str, str] = {}
    live = export / "artifacts_live"
    if not live.exists():
        return seen
    for d in live.glob("*"):
        md = d / "content.md"
        if md.exists():
            seen[_sha(md.read_bytes())] = d.name
    return seen


def refresh(export: Path) -> int:
    """Recompute duplicate/similarity metadata for rows already imported."""
    index = load_index(export)
    api = _api_hashes(export)
    profiles = _api_profiles(export)
    updated = 0
    for row in index:
        cdir = export / "artifacts_local" / f"{_slug(row['title'])}_{row['sha256'][:8]}"
        md = cdir / "content.md"
        if not md.exists():
            continue
        overlap_id, ratio = _best_match(_lines(md.read_text(encoding="utf-8", errors="replace")), profiles)
        before = (row.get("duplicate_of"), row.get("similar_to"), row.get("overlap"))
        row["duplicate_of"] = api.get(row["sha256"])
        row["similar_to"], row["overlap"] = overlap_id, ratio
        if before != (row["duplicate_of"], row["similar_to"], row["overlap"]):
            updated += 1
        write_atomic(cdir / "meta.json", json.dumps(row, ensure_ascii=False, indent=1))
    write_atomic(export / INDEX_NAME, json.dumps(index, ensure_ascii=False, indent=1))
    print(f"refreshed {updated}/{len(index)} entries")
    return updated


def import_dir(export: Path, source: Path, move: bool = False,
               pattern: str = "*.md", limit: int | None = None) -> dict:
    export.mkdir(parents=True, exist_ok=True)
    dest_root = export / "artifacts_local"
    dest_root.mkdir(parents=True, exist_ok=True)

    index = load_index(export)
    known = {row["sha256"] for row in index}
    api = _api_hashes(export)
    profiles = _api_profiles(export)

    files = sorted(p for p in source.glob(pattern) if p.is_file())
    if limit:
        files = files[:limit]

    imported = skipped = failed = 0
    for path in files:
        try:
            data = path.read_bytes()
            digest = _sha(data)
            if digest in known:
                print(f"  skip (already imported) {path.name[:56]}")
                skipped += 1
                continue

            title = path.stem
            cdir = dest_root / f"{_slug(title)}_{digest[:8]}"
            write_atomic(cdir / "content.md", data)
            text = data.decode("utf-8", "replace")
            write_atomic(cdir / "content.txt", text)

            similar_to, overlap = _best_match(_lines(text), profiles)
            row = {
                "title": title,
                "chars": len(text.strip()),
                "sha256": digest,
                "source": str(path),
                "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duplicate_of": api.get(digest),
                "similar_to": similar_to,
                "overlap": overlap,
            }
            write_atomic(cdir / "meta.json", json.dumps(row, ensure_ascii=False, indent=1))

            index.append(row)
            known.add(digest)
            imported += 1
            note = f" (duplicate of API artifact {api[digest][:12]})" if api.get(digest) else ""
            print(f"  + {title[:56]} {row['chars']} chars{note}")

            if move:
                path.unlink()
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {path.name[:56]}: {e}")
            failed += 1

    write_atomic(export / INDEX_NAME, json.dumps(index, ensure_ascii=False, indent=1))
    print(f"\nimported {imported}, skipped {skipped}, failed {failed} "
          f"(index: {len(index)} entries)")
    return {"imported": imported, "skipped": skipped, "failed": failed, "total": len(index)}
