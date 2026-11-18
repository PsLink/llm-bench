"""Publish complete BFCL archives atomically on worker/API shared storage."""

from pathlib import Path
import re
import tarfile
from uuid import uuid4


def persist_artifacts(directory, submission_id, run_id, index):
    index = Path(index)
    if index.name != "artifacts.json" or not index.is_file():
        raise ValueError("BFCL attempt has no artifact index")
    if not re.fullmatch(r"[a-f0-9]{32}", index.parent.name):
        raise ValueError("Invalid BFCL attempt identity")
    destination = Path(directory).resolve() / str(submission_id) / str(run_id)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{index.parent.name}.tar.gz"
    scratch = destination / f".{uuid4().hex}.tmp"
    try:
        with tarfile.open(scratch, "w:gz") as tar:
            for file in sorted(index.parent.rglob("*")):
                if (
                    file.is_file()
                    and not file.is_symlink()
                    and file.name != "config.json"
                ):
                    tar.add(
                        file, arcname=file.relative_to(index.parent), recursive=False
                    )
        scratch.replace(archive)
    finally:
        scratch.unlink(missing_ok=True)
    return archive


def downloadable_artifact(directory, submission_id, run_id, value):
    if not directory or not value:
        return None
    expected = Path(directory).resolve() / str(submission_id) / str(run_id)
    path = Path(value).resolve()
    if (
        path.parent == expected
        and re.fullmatch(r"[a-f0-9]{32}\.tar\.gz", path.name)
        and path.is_file()
    ):
        return path
    return None
