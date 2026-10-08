"""
Regenerate the committed sample compliance report.

The sample is built through run_store, so the JSONL write path is
hash-chained before compliance_report renders the HTML. The target,
reviewers and timings are fictional and `generated_at` is pinned; the
head hash still changes on every regeneration, because each log entry
is timestamped when it is written.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compliance_report import generate_for_run
from run_store import (
    append_approval_record,
    append_notification_attempt,
    artifact_ref_for_draft,
    save_snapshot,
)


RUN_ID = "a5042cea-6c6b-408d-afdd-a399a385914b"
GENERATED_AT = "2026-05-16T11:10:00+00:00"
# (draft role, reviewer, decided_at, rationale) — fictional people, reserved domain.
DECISIONS = (
    ("economic_buyer", "m.rossi@example.com", "2026-05-16T10:42:10+00:00",
     "claims match the cited annual report and press release"),
    ("legal_compliance", "l.bianchi@example.com", "2026-05-16T11:07:30+00:00",
     "regulatory reference verified against the cited source"),
)


def sample_state() -> dict:
    return {
        "run_id": RUN_ID,
        "target_account": {
            "legal_name": "Example Bank S.p.A.",
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
            {
                "role": "economic_buyer",
                "backing_claim_ids": ["k1", "k2"],
            },
            {
                "role": "legal_compliance",
                "backing_claim_ids": ["k3"],
            },
        ],
        "outreach_drafts": [
            {"role": "economic_buyer", "backing_claim_ids": ["k1"]},
            {"role": "legal_compliance", "backing_claim_ids": ["k3"]},
        ],
    }


def build_sample(output_path: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="abm_sample_") as tmp:
        return _build(Path(tmp), output_path)


def _build(store_dir: Path, output_path: Path) -> Path:
    save_snapshot(store_dir, sample_state())
    append_notification_attempt(
        store_dir,
        RUN_ID,
        {
            "kind": "review_requested",
            "channel": "null",
            "outcome": "delivered",
            "attempted_at": "2026-05-16T10:00:01+00:00",
            "run_id": RUN_ID,
        },
    )
    for role, reviewer, decided_at, rationale in DECISIONS:
        append_approval_record(
            store_dir,
            RUN_ID,
            {
                "artifact_ref": artifact_ref_for_draft(RUN_ID, role),
                "decision": "approved",
                "decided_by": reviewer,
                "decided_at": decided_at,
                "rationale": rationale,
            },
        )

    return generate_for_run(
        store_dir / f"{RUN_ID}.jsonl", output_path, generated_at=GENERATED_AT
    )


def main() -> int:
    out = build_sample(ROOT / "abm_compliance_report.html")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
