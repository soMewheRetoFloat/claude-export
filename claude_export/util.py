"""Small shared helpers."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_UNSAFE = re.compile(r"[^\w\u4e00-\u9fff\-./]+", re.UNICODE)


def write_atomic(path: Path, data: str | bytes) -> None:
    """Write via a temp file + rename so an interrupted run never leaves a
    truncated file that a later run would mistake for a complete one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".part")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        if isinstance(data, bytes):
            tmp_path.write_bytes(data)
        else:
            tmp_path.write_text(data, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def safe_relative(base: Path, rel: str, fallback: str = "file.txt") -> Path:
    """Join `rel` under `base`, dropping any component that could escape it.

    Paths here come from model-generated tool input (`create_file` etc.), so
    `../` must never be able to write outside the export directory.
    """
    clean = _UNSAFE.sub("_", str(rel)).replace("\\", "/")
    parts = [p for p in clean.split("/") if p not in ("", ".", "..")]
    if not parts:
        parts = [fallback]
    return base.joinpath(*parts)
