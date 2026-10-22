"""Resolve request-controlled paths inside one saved job on Windows and Linux."""
from pathlib import Path, PurePosixPath
import re


def job_directory(root, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
        return None
    try:
        root = Path(root).resolve()
        path = root / job_id
        if path.is_symlink() or path.resolve().parent != root:
            return None
        return path
    except (OSError, ValueError, RuntimeError):
        return None


def contained_path(root, relative):
    """Return a confined path or None; existence is checked by the caller."""
    try:
        name = str(relative)
        raw = PurePosixPath(name)
        if not name or any(c in name for c in ('\\', ':', '\x00')) or raw.is_absolute() or '..' in raw.parts:
            return None
        root = Path(root).resolve()
        path = root.joinpath(*raw.parts)
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            return None
        return path
    except (OSError, ValueError, RuntimeError):
        return None
