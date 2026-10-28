#!/usr/bin/env python3
"""Build a clean, deterministic release ZIP: source, migrations, models and docs only.

Excluded on purpose: generated databases, logs, caches, bytecode, generated evaluation splits, local object storage,
virtualenvs, VCS metadata and any real secret/env file (``.env.example`` is kept). Usage:
    python scripts/make_release.py [SOURCE_DIR] [OUTPUT_ZIP]
"""
from __future__ import annotations

import fnmatch
import sys
import zipfile
from pathlib import Path

EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git", ".venv", "venv", "private_storage", "logs"}
EXCLUDED_PATHS = {"data/splits"}                                   # generated, seeded, rebuilt by tests/CI
EXCLUDED_FILES = ["*.pyc", "*.pyo", "*.db", "*.sqlite", "*.sqlite3", "*.db-journal", "*.db-wal", "*.db-shm", "*.log", "*.pem", "*.key", ".env", ".env.*", ".DS_Store"]
KEPT_FILES = {".env.example"}
FIXED_TIME = (2026, 1, 1, 0, 0, 0)                                 # deterministic archives


def is_excluded(rel: Path) -> bool:
    posix = rel.as_posix()
    if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]) or any(posix == p or posix.startswith(p + "/") for p in EXCLUDED_PATHS):
        return True
    if rel.name in KEPT_FILES:
        return False
    return any(fnmatch.fnmatch(rel.name, pat) for pat in EXCLUDED_FILES)


def build_release(src: Path, dest: Path, root_name: str | None = None) -> list[str]:
    src = Path(src).resolve(); dest = Path(dest).resolve(); root = root_name or src.name
    included: list[str] = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            rel = path.relative_to(src)
            if is_excluded(rel) or path.resolve() == dest:
                continue
            info = zipfile.ZipInfo(f"{root}/{rel.as_posix()}", FIXED_TIME); info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (path.stat().st_mode & 0xFFFF) << 16
            z.writestr(info, path.read_bytes()); included.append(rel.as_posix())
    return included


if __name__ == "__main__":
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else source.parent / f"{source.name}_release.zip"
    files = build_release(source, out)
    print(f"{out}: {len(files)} files")
