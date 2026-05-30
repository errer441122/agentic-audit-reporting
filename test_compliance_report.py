"""
Archive-runnable tests for compliance_report.

This repository ships the integrity and reporting layer, not the
complete ABM agent pipeline. The test therefore builds a representative
run directly through run_store and verifies the generated report from
the resulting hash-chained JSONL.

Run:
    python test_compliance_report.py
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from compliance_report import compute_metrics, generate_for_run, render_html
from run_store import (
    append_approval_record,
    append_notification_attempt,
    artifact_ref_for_draft,
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
        "target_account": {
            "legal_name": "Intesa Sanpaolo",
            "country": "IT",
        },
        "supervisor_phase": "approved",
        "started_at": "2026-05-16T10:00:00+00:00",
        "citations": [
            {"id": "c1", "source": "annual_report"},
            {"id": "c2", "source": "press_release"},
            {"id": "c3", "source": "tier1_news"},
        ],
        "claims": [
            {"id": "k1", "citation_ids": ["c1"]},
            {"id": "k2", "citation_ids": ["c2"]},
            {"id": "k3", "citation_ids": ["c3"]},
        ],
        "dossiers": [
            {"role": "economic_buyer", "backing_claim_ids": ["k1", "k2"]},
            {"role": "legal_compliance", "backing_claim_ids": ["k3"]},
        ],
        "outreach_drafts": [
            {"role": "economic_buyer", "backing_claim_ids": ["k1"]},
            {"role": "legal_compliance", "backing_claim_ids": ["k3"]},
        ],
    }


def main() -> int:
    store_dir = Path(tempfile.mkdtemp(prefix="abm_report_"))
    run_id = "00000000-0000-0000-0000-000000000007"
    run_jsonl = store_dir / f"{run_id}.jsonl"

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
    for role in ("economic_buyer", "legal_compliance"):
        append_approval_record(
            store_dir,
            run_id,
            ApprovalRecord(
                artifact_ref=artifact_ref_for_draft(run_id, role),
                decision=Decision.APPROVED,
                decided_by="alice@firm.eu",
                decided_at="2026-05-16T10:00:02+00:00",
                rationale="fixture approval",
            ),
        )

    metrics = compute_metrics(run_jsonl)
    assert metrics.target_name == "Intesa Sanpaolo"
    assert metrics.final_phase == "approved"
    assert metrics.snapshots_count == 1
    assert metrics.approval_records_count == 2
    assert metrics.notification_attempts_count == 1
    assert metrics.citation_coverage == 1.0
    assert metrics.dossier_coverage == 1.0
    assert metrics.draft_coverage == 1.0
    assert metrics.hitl_coverage == 1.0
    assert metrics.reviewers == ("alice@firm.eu",)
    assert metrics.decisions_by_type == {"approved": 2}
    assert metrics.chain_status.valid is True
    assert metrics.chain_status.chain_present is True

    html = render_html(metrics)
    assert html.startswith("<!doctype html>")
    assert run_id in html
    assert "Intesa Sanpaolo" in html
    assert "Art. 12" in html
    assert "Art. 13" in html
    assert "Art. 14" in html
    assert "100.0%" in html
    assert "alice@firm.eu" in html
    assert "not a legal opinion" in html
    assert "Chain verified" in html
    assert "Chain not present" not in html

    out_path = generate_for_run(run_jsonl, store_dir / "report.html")
    assert out_path.exists()
    assert out_path.stat().st_size > 2000

    print("compliance_report assertions passed.")
    return 0


def test_compliance_report_from_chained_store() -> None:
    assert main() == 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
