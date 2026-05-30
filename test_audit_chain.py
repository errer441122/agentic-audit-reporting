"""
Standalone regression test for the hash-chain integrity layer and
the compliance report's chain-status rendering.

This file depends on audit_logger, compliance_report, and the standard
library. It does not need agent-pipeline modules.

Run:
    python test_audit_chain.py

Scenarios:

A. Chained + valid: append_chained x3, verify_chain is valid, and the
   report summary says "Chain verified".
B. Tampered: editing a middle entry breaks verification at that line.
C. Chain not present: legacy/plain JSONL has no prev_hash/entry_hash,
   so the report says "Chain not present" rather than "Chain broken".
D. Empty file: summary says "Chain empty".
E. End-to-end report: generate_for_run on legacy/plain JSONL preserves
   the honest "Chain not present" label.
F. Malformed JSON line: corrupt JSONL is reported as "Chain broken".
"""

import json
import sys
import tempfile
from pathlib import Path

from audit_logger import append_chained, read_chain, verify_chain
from compliance_report import _chain_summary, generate_for_run


def _section(title: str) -> None:
    print(f"\n--- {title} ---")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="abm_chain_"))

    # =====================================================
    # A. Chained + valid
    # =====================================================
    _section("A. Chained + valid")
    p = tmp / "chained.jsonl"
    h1 = append_chained(p, {"kind": "ev", "n": 1})
    h2 = append_chained(p, {"kind": "ev", "n": 2})
    h3 = append_chained(p, {"kind": "ev", "n": 3})
    assert h1 != h2 != h3
    chain = read_chain(p)
    assert len(chain) == 3
    assert chain[0].prev_hash == "0" * 64
    assert chain[1].prev_hash == chain[0].entry_hash
    assert chain[2].prev_hash == chain[1].entry_hash

    res = verify_chain(p)
    print(f"  valid={res.valid} present={res.chain_present} "
          f"checked={res.entries_checked}")
    assert res.valid
    assert res.chain_present
    assert res.entries_checked == 3
    label, css, _ = _chain_summary(res)
    print(f"  summary -> {label} / {css}")
    assert label == "Chain verified"
    assert css == "score-good"

    # =====================================================
    # B. Tampered middle entry
    # =====================================================
    _section("B. Tampered middle entry")
    lines = p.read_text(encoding="utf-8").splitlines()
    middle = json.loads(lines[1])
    middle["n"] = 99
    lines[1] = json.dumps(middle, sort_keys=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = verify_chain(p)
    print(f"  valid={res.valid} bad_line={res.first_bad_line} "
          f"reason={res.reason[:50]}…")
    assert not res.valid
    assert res.first_bad_line == 2
    assert "entry_hash mismatch" in res.reason
    label, css, _ = _chain_summary(res)
    print(f"  summary -> {label} / {css}")
    assert label == "Chain broken"
    assert css == "score-poor"

    # =====================================================
    # C. Chain not present (the shipped bug)
    # =====================================================
    _section("C. Chain not present")
    plain = tmp / "plain.jsonl"
    plain.write_text(
        "\n".join(
            json.dumps({"kind": "state_snapshot", "ts": "2026-05-16",
                        "state": {"run_id": "r1", "n": i}})
            for i in range(3)
        ) + "\n",
        encoding="utf-8",
    )
    res = verify_chain(plain)
    print(f"  valid={res.valid} present={res.chain_present} "
          f"checked={res.entries_checked}")
    # Not a failure: nothing was tampered, there is simply no chain.
    assert res.valid
    assert res.chain_present is False
    assert res.entries_checked == 3
    label, css, expl = _chain_summary(res)
    print(f"  summary -> {label} / {css}")
    assert label == "Chain not present", (
        f"REGRESSION: legacy/plain JSONL must render 'Chain not "
        f"present', got {label!r}"
    )
    assert css == "score-medium"
    assert "tamper-evidence is not available" in expl.lower()

    # =====================================================
    # D. Empty file
    # =====================================================
    _section("D. Empty file")
    empty = tmp / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    res = verify_chain(empty)
    label, css, _ = _chain_summary(res)
    print(f"  summary -> {label} / {css}")
    assert label == "Chain empty"

    # =====================================================
    # E. End-to-end: report on a legacy/plain run JSONL
    # =====================================================
    _section("E. End-to-end report on legacy/plain run")
    run_jsonl = tmp / "run.jsonl"
    state = {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "target_account": {"legal_name": "Acme S.p.A."},
        "supervisor_phase": "approved",
        "started_at": "2026-05-16T10:00:00+00:00",
        "citations": [{"id": "c1"}],
        "claims": [{"id": "k1", "citation_ids": ["c1"]}],
        "dossiers": [{"role": "legal_compliance",
                      "backing_claim_ids": ["k1"]}],
        "outreach_drafts": [{"role": "legal_compliance",
                             "backing_claim_ids": ["k1"]}],
    }
    run_jsonl.write_text(
        json.dumps({"kind": "state_snapshot", "ts": "2026-05-16",
                    "state": state}) + "\n",
        encoding="utf-8",
    )
    out = generate_for_run(run_jsonl, tmp / "report.html")
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "Chain not present" in html, \
        "report must render the legacy/plain run honestly"
    assert "Chain broken" not in html, (
        "REGRESSION: a legacy/plain run must NOT be reported as "
        "'Chain broken' (this was the original shipped defect)"
    )
    assert "not a legal opinion" in html
    print(f"  wrote {out.stat().st_size} bytes; chain rendered as "
          f"'Chain not present'")

    # =====================================================
    # F. Malformed JSON line (corrupt audit file)
    # =====================================================
    _section("F. Malformed JSON line")
    bad = tmp / "malformed.jsonl"
    bad.write_text(
        "\n".join([
            json.dumps({"kind": "ev", "entry_hash": "abc", "n": 0}),
            "not-json{",
            json.dumps({"kind": "ev", "entry_hash": "def", "n": 2}),
        ]) + "\n",
        encoding="utf-8",
    )
    # Must not raise json.JSONDecodeError — a corrupt audit file is
    # a verification failure, not a crash.
    res = verify_chain(bad)
    print(f"  valid={res.valid} bad_line={res.first_bad_line} "
          f"reason={res.reason[:60]}…")
    assert res.valid is False
    assert res.first_bad_line == 2, (
        f"malformed line is line 2, got {res.first_bad_line!r}"
    )
    assert "malformed" in res.reason.lower(), (
        f"reason must mention 'malformed', got {res.reason!r}"
    )
    # read_chain itself must not crash and must flag the bad line.
    chain = read_chain(bad)
    assert len(chain) == 3
    assert chain[1].malformed is True
    assert chain[0].malformed is False
    label, css, _ = _chain_summary(res)
    print(f"  summary -> {label} / {css}")
    assert label == "Chain broken", (
        f"a corrupt audit file must render 'Chain broken', "
        f"got {label!r}"
    )
    assert css == "score-poor"

    print("\nAll audit-chain assertions passed.")
    return 0


def test_audit_chain_scenarios() -> None:
    assert main() == 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
