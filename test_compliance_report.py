"""
Tests for audit_logger (hash-chained JSONL) and compliance_report
(EU AI Act Art. 12/13/14 metric pack).

Five scenarios:

A. Hash chain — append_chained then verify_chain on a synthetic
   chain. Three entries, three sequential prev_hash links, all
   verify cleanly.

B. Tampering detection — modify a middle entry's field, re-verify,
   expect first_bad_line at the modified line with a clear reason.
   Also: remove an entry, expect the next entry's prev_hash to
   mismatch.

C. compliance_report.compute_metrics on a real run JSONL produced
   by the full pipeline + approval flow. Assert citation_coverage,
   dossier_coverage, draft_coverage all == 1.0 on a clean run.
   HITL coverage == 1.0 after all decisions are posted.

D. compliance_report.render_html produces a self-contained HTML
   document (begins with <!doctype html>, contains the run ID and
   the target name, contains all three Art. headings).

E. The "chain not present" case: the run JSONL written by run_store
   today has no prev_hash/entry_hash fields. verify_chain reports
   that as valid=True but chain_present=False (it is the ABSENCE of
   a chain, not a tampered chain), and compliance_report renders
   chain_label="Chain not present" with the conservative wording.
   Asserting this here is the regression guard for the originally
   shipped report, which wrongly rendered "Chain broken".

Run:
    cd /home/claude/abm_module
    python test_compliance_report.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

# audit_logger is part of this partial archive and is always
# importable; the chain scenarios (A, B) only depend on it.
from audit_logger import (
    append_chained,
    read_chain,
    verify_chain,
)

# Everything below requires the full pipeline (and fastapi), which
# is NOT present in this partial export. Import lazily and degrade
# gracefully instead of crashing at import time with a traceback.
_MISSING_MODULE: str | None = None
try:
    from fastapi.testclient import TestClient

    from abm_state import TargetAccount, new_run
    from approval_api import create_app
    from approval_gate import approval_gate
    from claim_extractor import PatternClaimExtractor
    from committee_mapper import committee_mapper
    from compliance_report import (
        compute_metrics,
        render_html,
        generate_for_run,
    )
    from dossier_writer import dossier_writer
    from notifications import InMemoryNotifier
    from outreach_drafter import outreach_drafter
    from run_store import save_snapshot
    from signal_collector import FixturesAdapter, signal_collector

    _PIPELINE_AVAILABLE = True
except ModuleNotFoundError as exc:
    _PIPELINE_AVAILABLE = False
    _MISSING_MODULE = exc.name


def build_fixture_corpus(root: Path, account_name: str) -> None:
    account_dir = root / account_name.replace(" ", "_")
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / "sec_filing__10k_2025.txt").write_text(
        "Intesa Sanpaolo S.p.A. EU AI Act model risk governance "
        "Chief Financial Officer.\n",
        encoding="utf-8",
    )
    (account_dir / "press_release__digital_strategy_2026.txt").write_text(
        "Intesa Sanpaolo deploy agentic AI compliance marketing "
        "operations human-in-the-loop.\n",
        encoding="utf-8",
    )
    (account_dir / "news_tier1__reuters_2026-01.txt").write_text(
        "Intesa Sanpaolo expanding AI governance responsible-AI "
        "specialists banking supervisor.\n",
        encoding="utf-8",
    )


def _section(title: str) -> None:
    print(f"\n--- {title} ---")


def main() -> int:
    if not _PIPELINE_AVAILABLE:
        print(
            "SKIPPED: test_compliance_report.py requires the full "
            "pipeline modules (abm_state, run_store, approval_api, "
            "claim_extractor, committee_mapper, dossier_writer, "
            "outreach_drafter, notifications, signal_collector) and "
            "fastapi, which are not present in this partial archive "
            f"(first missing: {_MISSING_MODULE!r}). Run "
            "`python test_audit_chain.py` instead."
        )
        return 0

    fixtures_root = Path(tempfile.mkdtemp(prefix="abm_cr_"))
    store_dir = Path(tempfile.mkdtemp(prefix="abm_cr_store_"))
    out_dir = Path(tempfile.mkdtemp(prefix="abm_cr_out_"))
    build_fixture_corpus(fixtures_root, "Intesa Sanpaolo")

    try:
        # =====================================================
        # A. Hash chain on a synthetic JSONL
        # =====================================================
        _section("A. Hash chain — synthetic three-entry chain")

        chain_path = store_dir / "synthetic.jsonl"
        h1 = append_chained(chain_path, {"kind": "ev", "n": 1})
        h2 = append_chained(chain_path, {"kind": "ev", "n": 2})
        h3 = append_chained(chain_path, {"kind": "ev", "n": 3})
        print(f"  hashes: {h1[:12]}…  {h2[:12]}…  {h3[:12]}…")
        assert h1 != h2 != h3

        chain = read_chain(chain_path)
        assert len(chain) == 3
        # First entry's prev is genesis.
        assert chain[0].prev_hash == "0" * 64
        # Each subsequent entry's prev matches the previous entry_hash.
        assert chain[1].prev_hash == chain[0].entry_hash
        assert chain[2].prev_hash == chain[1].entry_hash

        result = verify_chain(chain_path)
        print(f"  verify: valid={result.valid}, "
              f"checked={result.entries_checked}")
        assert result.valid
        assert result.entries_checked == 3

        # =====================================================
        # B. Tampering detection
        # =====================================================
        _section("B. Tamper detection")

        # B.1: modify a field of the middle entry.
        lines = chain_path.read_text(encoding="utf-8").splitlines()
        middle = json.loads(lines[1])
        middle["n"] = 99   # change the payload
        lines[1] = json.dumps(middle, sort_keys=True)
        chain_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_chain(chain_path)
        print(f"  after field edit: valid={result.valid}, "
              f"bad_line={result.first_bad_line}, "
              f"reason={result.reason[:60]}…")
        assert not result.valid
        # The modified line's entry_hash no longer matches its
        # recomputed canonical content -> self-hash mismatch on line 2.
        assert result.first_bad_line == 2
        assert "entry_hash mismatch" in result.reason

        # B.2: remove the middle entry entirely.
        chain_path2 = store_dir / "synthetic2.jsonl"
        append_chained(chain_path2, {"kind": "ev", "n": 1})
        append_chained(chain_path2, {"kind": "ev", "n": 2})
        append_chained(chain_path2, {"kind": "ev", "n": 3})

        lines2 = chain_path2.read_text(encoding="utf-8").splitlines()
        # Drop line 2 (index 1).
        del lines2[1]
        chain_path2.write_text("\n".join(lines2) + "\n", encoding="utf-8")

        result = verify_chain(chain_path2)
        print(f"  after deletion: valid={result.valid}, "
              f"bad_line={result.first_bad_line}, "
              f"reason={result.reason[:60]}…")
        assert not result.valid
        # The new line 2 (originally line 3) has a prev_hash pointing
        # to the deleted entry; that's not equal to line 1's entry_hash.
        assert result.first_bad_line == 2
        assert "prev_hash mismatch" in result.reason

        # =====================================================
        # C. compute_metrics on a real run JSONL
        # =====================================================
        _section("C. compute_metrics on a real run")

        state = new_run(TargetAccount(
            legal_name="Intesa Sanpaolo", country="IT", ticker="ISP.MI",
        ))
        state["citations"] = signal_collector(
            state, adapters=[FixturesAdapter(root=fixtures_root)]
        )["citations"]
        state["claims"] = PatternClaimExtractor().extract(state["citations"])
        state["committee"] = committee_mapper(state)["committee"]
        state["dossiers"] = dossier_writer(state)["dossiers"]
        state["outreach_drafts"] = outreach_drafter(state)["outreach_drafts"]
        save_snapshot(store_dir, state)

        # Post approvals.
        app = create_app(store_dir=store_dir)
        client = TestClient(app)
        seen: set[str] = set()
        for d in state["outreach_drafts"]:
            if d.role.value in seen:
                continue
            seen.add(d.role.value)
            r = client.post(
                f"/api/runs/{state['run_id']}/decisions",
                json={"role": d.role.value, "decision": "approved",
                      "decided_by": "alice@firm.eu", "rationale": "ok"},
            )
            assert r.status_code == 200

        # Run the gate so the audit trail includes the notification
        # attempts.
        ticks = [0.0]
        approval_gate(
            state,
            store_dir=store_dir,
            timeout_s=5.0,
            poll_interval_s=0.1,
            notifier=InMemoryNotifier(),
            clock=lambda: ticks[0],
            sleep=lambda dt: ticks.__setitem__(0, ticks[0] + dt),
        )

        run_jsonl = store_dir / f"{state['run_id']}.jsonl"
        metrics = compute_metrics(run_jsonl)

        print(f"  target:                  {metrics.target_name}")
        print(f"  final phase:             {metrics.final_phase}")
        print(f"  snapshots:               {metrics.snapshots_count}")
        print(f"  approvals:               {metrics.approval_records_count}")
        print(f"  notification attempts:   "
              f"{metrics.notification_attempts_count}")
        print(f"  citations:               {metrics.citations_count}")
        print(f"  claims:                  {metrics.claims_count}")
        print(f"  citation coverage:       "
              f"{metrics.citation_coverage * 100:.1f}%")
        print(f"  dossier coverage:        "
              f"{metrics.dossier_coverage * 100:.1f}%")
        print(f"  draft coverage:          "
              f"{metrics.draft_coverage * 100:.1f}%")
        print(f"  HITL coverage:           "
              f"{metrics.hitl_coverage * 100:.1f}%")
        print(f"  reviewers:               "
              f"{', '.join(metrics.reviewers)}")
        print(f"  decisions:               {metrics.decisions_by_type}")

        # The pattern-pipeline produces 100% provenance by
        # construction. Anything less means the pipeline regressed.
        assert metrics.target_name == "Intesa Sanpaolo"
        assert metrics.final_phase == "approved"
        assert metrics.citation_coverage == 1.0, (
            f"expected 100% citation coverage, got "
            f"{metrics.citation_coverage}"
        )
        assert metrics.dossier_coverage == 1.0
        assert metrics.draft_coverage == 1.0
        assert metrics.hitl_coverage == 1.0, (
            f"expected 100% HITL coverage on this run"
        )
        assert metrics.reviewers == ("alice@firm.eu",)
        assert metrics.decisions_by_type.get("approved", 0) >= 1

        # =====================================================
        # D. HTML rendering structural assertions
        # =====================================================
        _section("D. HTML rendering")

        html = render_html(metrics)
        assert html.startswith("<!doctype html>"), \
            "report must start with a proper doctype"
        assert metrics.run_id in html, "run id must appear"
        assert "Intesa Sanpaolo" in html, "target name must appear"
        assert "Art. 12" in html, "Article 12 heading must be present"
        assert "Art. 13" in html, "Article 13 heading must be present"
        assert "Art. 14" in html, "Article 14 heading must be present"
        assert "100.0%" in html, \
            "100% coverage metrics should be visible in the HTML"
        assert "alice@firm.eu" in html, \
            "reviewer identities must appear in the report"
        # The disclaimer must be present so the report doesn't
        # overclaim certification.
        assert "not a legal opinion" in html, \
            "report must include the legal-opinion disclaimer"
        # Regression guard (scenario E in the module docstring):
        # this run JSONL is written by run_store with no hash chain,
        # so the report must say "Chain not present" — NOT
        # "Chain verified" (nothing to verify) and NOT "Chain
        # broken" (nothing was tampered; the original shipped report
        # got this wrong).
        assert "Chain not present" in html, \
            "un-chained run must render chain status 'Chain not present'"
        assert "Chain broken" not in html, \
            "un-chained run must not be reported as a broken chain"

        # =====================================================
        # E. generate_for_run writes the file
        # =====================================================
        _section("E. generate_for_run writes the file")
        out_path = out_dir / "report.html"
        written = generate_for_run(run_jsonl, out_path)
        assert written.exists()
        assert written.stat().st_size > 2000, \
            "report seems suspiciously small"
        print(f"  wrote {written.stat().st_size} bytes to {written.name}")

        # Save a copy for the user to inspect.
        permanent = Path("/tmp/abm_compliance_report.html")
        shutil.copy(written, permanent)
        print(f"  saved a copy for inspection: {permanent}")

        print("\nAll compliance-report assertions passed.")
        return 0
    finally:
        shutil.rmtree(fixtures_root, ignore_errors=True)
        shutil.rmtree(store_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
