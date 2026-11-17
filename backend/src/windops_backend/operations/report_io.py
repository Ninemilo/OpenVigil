from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, TextIO


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_json_receipt(payload: dict[str, Any], stream: TextIO, *, pretty: bool = False) -> None:
    if pretty:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    else:
        json.dump(payload, stream, allow_nan=False)


def write_new_json(path: Path, payload: dict[str, Any], *, pretty: bool = False) -> None:
    """Create a receipt exclusively in compact or readable form."""
    with path.open("x", encoding="utf-8") as stream:
        dump_json_receipt(payload, stream, pretty=pretty)


def write_atomic_json(
    path: Path,
    payload: dict[str, Any],
    *,
    symlink_error: Exception,
    temporary_suffix: str = "",
) -> None:
    if path.is_symlink():
        raise symlink_error
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=temporary_suffix,
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
