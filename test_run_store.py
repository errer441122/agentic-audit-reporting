"""
Regression tests for the archive-local run_store write path.

These tests intentionally exercise only the shipped integrity and
reporting layer: no agent pipeline, no FastAPI, no external services.
"""

from __future__ import annotations

import sys
import tempfile
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from audit_logger import read_chain, verify_chain
from compliance_report import compute_metrics, generate_for_run
from run_store import (
    append_approval_record,
    append_notification_attempt,
    list_runs,
    load_latest,
    save_snapshot,
)


class Decision(Enum):
    APPROVED = "approved"


@dataclass(frozen=True)
class ApprovalRecord:
    artifact_ref: str
    decision: Decision
    decided_by: str
    decided_at: str
    rationale: str


@dataclass(frozen=True)
class NotificationAttempt:
    kind: str
    channel: str
    outcome: str
    attempted_at: str
    run_id: str


def _state(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "target_account": {"legal_name": "Acme S.p.A.", "country": "IT"},
        "supervisor_phase": "approved",
        "started_at": "2026-05-16T10:00:00+00:00",
        "citations": [{"id": "c1"}],
        "claims": [{"id": "k1", "citation_ids": ["c1"]}],
        "dossiers": [{
            "role": "legal_compliance",
            "backing_claim_ids": ["k1"],
        }],
        "outreach_drafts": [{
            "role": "legal_compliance",
            "backing_claim_ids": ["k1"],
        }],
    }


def main() -> int:
    store_dir = Path(tempfile.mkdtemp(prefix="abm_run_store_"))
    run_id = "00000000-0000-0000-0000-000000000042"
    run_path = store_dir / f"{run_id}.jsonl"

    save_snapshot(store_dir, _state(run_id))
    append_notification_attempt(
        store_dir,
        run_id,
        NotificationAttempt(
            kind="review_requested",
            channel="null",
            outcome="delivered",
            attempted_at="2026-05-16T10:00:01+00:00",
            run_id=run_id,
        ),
    )
    append_approval_record(
        store_dir,
        run_id,
        ApprovalRecord(
            artifact_ref=f"draft:{run_id}:legal_compliance",
            decision=Decision.APPROVED,
            decided_by="alice@firm.eu",
            decided_at="2026-05-16T10:00:02+00:00",
            rationale="ok",
        ),
    )

    chain = read_chain(run_path)
    assert [entry.kind for entry in chain] == [
        "state_snapshot",
        "notification_attempt",
        "approval_record",
    ]
    assert all(entry.entry_hash for entry in chain)
    assert verify_chain(run_path).valid is True
    assert verify_chain(run_path).chain_present is True

    metrics = compute_metrics(run_path)
    assert metrics.chain_status.valid is True
    assert metrics.chain_status.chain_present is True
    assert metrics.approval_records_count == 1
    assert metrics.notification_attempts_count == 1
    assert metrics.hitl_coverage == 1.0

    latest = load_latest(store_dir, run_id)
    assert latest["run_id"] == run_id
    assert len(latest["approvals"]) == 1
    assert latest["approvals"][0]["decision"] == "approved"
    assert list_runs(store_dir) == [run_id]

    out = generate_for_run(run_path, store_dir / "report.html")
    html = out.read_text(encoding="utf-8")
    assert "Chain verified" in html
    assert "Chain not present" not in html

    tampered_lines = run_path.read_text(encoding="utf-8").splitlines()
    tampered_entry = json.loads(tampered_lines[1])
    tampered_entry["attempt"]["outcome"] = "failed"
    tampered_lines[1] = json.dumps(tampered_entry, sort_keys=True)
    run_path.write_text("\n".join(tampered_lines) + "\n", encoding="utf-8")

    tampered_status = verify_chain(run_path)
    assert tampered_status.valid is False
    assert tampered_status.first_bad_line == 2
    assert "entry_hash mismatch" in tampered_status.reason

    tampered_html = generate_for_run(
        run_path,
        store_dir / "tampered_report.html",
    ).read_text(encoding="utf-8")
    assert "Chain broken" in tampered_html
    assert "Chain verified" not in tampered_html

    print("run_store chained-write assertions passed.")
    return 0


def test_run_store_writes_chained_audit_events() -> None:
    assert main() == 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
