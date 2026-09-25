"""
run_store
=========

Archive-local JSONL persistence for the integrity and reporting layer.

The full ABM pipeline is intentionally not included in this repository.
This module provides the small store surface the shipped audit/report
code needs:

- append state snapshots
- append human approval records
- append notification attempts
- list and read run JSONL files

Every write goes through audit_logger.append_chained(), so a report
generated from this store can legitimately render "Chain verified".
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

_STORE_LOCK = threading.Lock()


def now_iso() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def artifact_ref_for_draft(run_id: str, role: str) -> str:
    """Canonical approval key for a draft role within a run."""
    return f"draft:{run_id}:{role}"


def run_path(store_dir: Path, run_id: str) -> Path:
    """Return the JSONL path for one run.

    run_id comes from pipeline state, so it is refused if it could
    escape store_dir (``../x``, ``a/b``, absolute paths): only
    ``[A-Za-z0-9._-]`` is allowed, which covers UUID run ids.
    """
    run_id = str(run_id)
    if run_id in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return Path(store_dir) / f"{run_id}.jsonl"


def _to_plain(value: Any) -> Any:
    """Convert common app objects into JSON-serializable primitives."""
    if is_dataclass(value):
        return _to_plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return _to_plain(value.model_dump())
    if hasattr(value, "dict") and callable(value.dict):
        return _to_plain(value.dict())
    return value


def _append_entry(store_dir: Path, run_id: str, entry: dict) -> str:
    # Local import avoids a circular import when audit_logger imports
    # _STORE_LOCK and now_iso from this module at startup.
    from audit_logger import append_chained

    return append_chained(run_path(store_dir, run_id), entry)


def save_snapshot(store_dir: Path, state: Mapping[str, Any]) -> str:
    """Append a state snapshot to the run's chained JSONL."""
    plain_state = _to_plain(state)
    run_id = plain_state.get("run_id")
    if not run_id:
        raise ValueError("state snapshot requires state['run_id']")
    return _append_entry(
        store_dir,
        str(run_id),
        {"kind": "state_snapshot", "state": plain_state},
    )


def append_approval_record(
    store_dir: Path,
    run_id: str,
    record: Any,
) -> str:
    """Append one human approval decision to the run's chained JSONL."""
    return _append_entry(
        store_dir,
        run_id,
        {"kind": "approval_record", "record": _to_plain(record)},
    )


def append_notification_attempt(
    store_dir: Path,
    run_id: str,
    attempt: Any,
    *,
    sub_attempts: list[Any] | None = None,
) -> str:
    """Append one notification attempt and optional per-channel attempts."""
    entry: dict[str, Any] = {
        "kind": "notification_attempt",
        "attempt": _to_plain(attempt),
    }
    if sub_attempts:
        entry["sub_attempts"] = _to_plain(sub_attempts)
    return _append_entry(store_dir, run_id, entry)


def read_entries(store_dir: Path, run_id: str) -> list[dict]:
    """Read raw JSONL entries for one run."""
    path = run_path(store_dir, run_id)
    if not path.exists():
        return []
    with _STORE_LOCK:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def load_latest(store_dir: Path, run_id: str) -> dict:
    """
    Return the latest state snapshot plus every approval record in the run.

    Approval records are returned as plain dictionaries under
    state["approvals"], which keeps this archive independent from the
    full pipeline's dataclass definitions.
    """
    entries = read_entries(store_dir, run_id)
    snapshots = [
        entry["state"]
        for entry in entries
        if entry.get("kind") == "state_snapshot"
    ]
    if not snapshots:
        raise FileNotFoundError(f"no state_snapshot entries for run {run_id}")

    latest = dict(snapshots[-1])
    approvals = [
        entry.get("record", {})
        for entry in entries
        if entry.get("kind") == "approval_record"
    ]
    latest["approvals"] = approvals
    return latest


def list_runs(store_dir: Path) -> list[str]:
    """List run IDs with JSONL files in store_dir."""
    root = Path(store_dir)
    if not root.exists():
        return []
    return sorted(path.stem for path in root.glob("*.jsonl"))
