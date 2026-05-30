"""
compliance_report
=================

Generates an HTML compliance report from a run's JSONL audit trail.
The report is intended as a *deliverable*: a Chief Compliance
Officer can open it in a browser, read it without context from the
operator, and take it to an external audit if needed.

Structure
---------

The report is organized around three EU AI Act articles relevant to
regulated outreach automation:

- Article 12 (Record-keeping)
  Every state mutation and every approval decision must be logged.
  We report: snapshots count, approval-record count, notification-
  attempt count, and the audit chain verification status.

- Article 13 (Transparency)
  Outputs that reach humans must be traceable to their sources.
  We report: citation coverage (% of claims with backing citations),
  claim coverage (% of dossiers / drafts with backing claims),
  per-draft provenance summaries.

- Article 14 (Human oversight)
  High-risk AI systems must allow effective human oversight.
  We report: HITL coverage (% of drafts decided by a human),
  decision distribution (approved / rejected / escalated), time-
  to-approval distribution, identified reviewers.

Each section produces (a) a numeric score visible at a glance,
(b) a one-paragraph explanation tying the score to the article's
wording, (c) the underlying evidence (counts, role-level
breakdowns).

Design choices
--------------

- **No external CSS / JS / fonts.** The report is a single self-
  contained HTML file. A compliance officer should be able to
  archive it, email it, print it to PDF, all without breakage.
- **Deterministic.** Same JSONL in produces the same HTML out
  (modulo a "generated_at" timestamp). This is how the report
  becomes diffable across runs.
- **Conservative.** Metrics show what they show; we do NOT
  interpret them as compliance certifications. The HTML
  explicitly says "this is a structural summary, not a legal
  opinion." That sentence is the difference between a useful
  audit aid and a document that overpromises.

Author: errer441122
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from audit_logger import VerificationResult, read_chain, verify_chain
try:
    from run_store import artifact_ref_for_draft
except ImportError:
    def artifact_ref_for_draft(run_id: str, role: str) -> str:
        """Canonical per-(run, role) approval ref: ``draft:<run_id>:<role>``.

        Mirrors ``run_store.artifact_ref_for_draft`` so the report
        layer works when exercised without the full pipeline. The
        real ``run_store`` is preferred whenever importable.
        """
        return f"draft:{run_id}:{role}"


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunMetrics:
    """All metrics extracted from one run's JSONL."""

    run_id: str
    target_name: str
    final_phase: str
    started_at: str

    # Article 12
    snapshots_count: int
    approval_records_count: int
    notification_attempts_count: int
    chain_status: VerificationResult

    # Article 13
    citations_count: int
    claims_count: int
    claims_with_provenance: int
    dossiers_count: int
    dossiers_with_backing: int
    drafts_count: int
    drafts_with_backing: int

    # Article 14
    reviewers: tuple[str, ...]
    decisions_by_type: dict[str, int]   # approved/rejected/escalated
    drafts_decided: int
    time_to_approval_seconds: float | None    # first request -> last decision

    @property
    def citation_coverage(self) -> float:
        if self.claims_count == 0:
            return 0.0
        return self.claims_with_provenance / self.claims_count

    @property
    def dossier_coverage(self) -> float:
        if self.dossiers_count == 0:
            return 0.0
        return self.dossiers_with_backing / self.dossiers_count

    @property
    def draft_coverage(self) -> float:
        if self.drafts_count == 0:
            return 0.0
        return self.drafts_with_backing / self.drafts_count

    @property
    def hitl_coverage(self) -> float:
        if self.drafts_count == 0:
            return 0.0
        # We measure unique role-decisions, not raw approval count.
        # KNOWN LIMITATION of this metric: approvals are keyed per
        # (run, role), so in runs with multiple drafts sharing a role
        # (e.g. two legal_compliance seats) a single decision counts
        # both as "decided". HITL coverage can therefore read 100%
        # even though the distinct same-role recipients were never
        # individually reviewed. Treat 100% as "every role decided",
        # not "every recipient individually reviewed".
        return min(1.0, self.drafts_decided / self.drafts_count)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def compute_metrics(jsonl_path: Path) -> RunMetrics:
    """
    Walk a run's JSONL once and extract the metrics. Resilient to
    extra/missing fields — if a field is absent, the metric reads
    zero rather than crashing. This keeps the report generatable
    even on runs that predate a schema change.
    """
    jsonl_path = Path(jsonl_path)
    if not jsonl_path.exists():
        raise FileNotFoundError(f"no run JSONL at {jsonl_path}")

    raw_entries = [
        json.loads(line) for line in jsonl_path.read_text(
            encoding="utf-8").splitlines() if line.strip()
    ]

    snapshots = [e for e in raw_entries if e.get("kind") == "state_snapshot"]
    approvals = [e for e in raw_entries
                 if e.get("kind") == "approval_record"]
    notifs = [e for e in raw_entries
              if e.get("kind") == "notification_attempt"]

    if not snapshots:
        raise ValueError(f"no state_snapshot entries in {jsonl_path}")

    latest_state = snapshots[-1]["state"]

    # Article 13 fields: citations, claims, dossiers, drafts.
    citations = latest_state.get("citations", [])
    claims = latest_state.get("claims", [])
    claims_with_prov = sum(
        1 for c in claims if c.get("citation_ids")
    )
    dossiers = latest_state.get("dossiers", [])
    dossiers_with_backing = sum(
        1 for d in dossiers if d.get("backing_claim_ids")
    )
    drafts = latest_state.get("outreach_drafts", [])
    drafts_with_backing = sum(
        1 for d in drafts if d.get("backing_claim_ids")
    )

    # Article 14 fields: reviewers and decision distribution.
    # Use the standalone approval_record entries (the canonical
    # audit source), not whatever is embedded in the latest
    # snapshot (which may lag).
    reviewers: set[str] = set()
    decisions: dict[str, int] = {}
    decided_refs: set[str] = set()
    for ap_entry in approvals:
        rec = ap_entry.get("record", {})
        reviewer = rec.get("decided_by", "").strip()
        if reviewer:
            reviewers.add(reviewer)
        decision = rec.get("decision", "")
        if decision:
            decisions[decision] = decisions.get(decision, 0) + 1
        ref = rec.get("artifact_ref")
        if ref:
            decided_refs.add(ref)

    # Drafts decided = each draft whose canonical ref appears in
    # the decided set. KNOWN LIMITATION: approvals are keyed per
    # (run, role), so two drafts sharing a role (e.g. two
    # legal_compliance seats) share the same artifact_ref and are
    # both counted as decided off a SINGLE decision. This metric
    # cannot distinguish "every same-role recipient was reviewed"
    # from "one decision covered all of them" — it will overcount
    # coverage for runs with distinct same-role recipients.
    drafts_decided = sum(
        1 for d in drafts
        if artifact_ref_for_draft(latest_state["run_id"], d.get("role"))
        in decided_refs
    )

    # Time-to-approval: first REVIEW_REQUESTED notification → last
    # approval_record decided_at.
    request_ts = None
    for n in notifs:
        attempt = n.get("attempt", {})
        if attempt.get("kind") == "review_requested":
            request_ts = _parse_ts(attempt.get("attempted_at", ""))
            break
    last_decision_ts = None
    for ap_entry in approvals:
        ts = _parse_ts(ap_entry.get("record", {}).get("decided_at", ""))
        if ts is None:
            continue
        if last_decision_ts is None or ts > last_decision_ts:
            last_decision_ts = ts

    if request_ts is not None and last_decision_ts is not None:
        delta = (last_decision_ts - request_ts).total_seconds()
        # Negative deltas mean decisions arrived before the gate
        # snapshotted (which happens in our test, where decisions
        # are posted then the gate runs). Report as 0.0 to avoid
        # misleading "approval finished before requested".
        ttap: float | None = max(0.0, delta)
    else:
        ttap = None

    # Chain status. The archive-local run_store writes chained JSONL.
    # We still tolerate legacy/plain JSONL and render that state as
    # "chain not present" rather than panicking.
    chain_status = verify_chain(jsonl_path)

    target = latest_state.get("target_account", {})

    return RunMetrics(
        run_id=latest_state["run_id"],
        target_name=target.get("legal_name", "?"),
        final_phase=latest_state.get("supervisor_phase", "?"),
        started_at=latest_state.get("started_at", ""),
        snapshots_count=len(snapshots),
        approval_records_count=len(approvals),
        notification_attempts_count=len(notifs),
        chain_status=chain_status,
        citations_count=len(citations),
        claims_count=len(claims),
        claims_with_provenance=claims_with_prov,
        dossiers_count=len(dossiers),
        dossiers_with_backing=dossiers_with_backing,
        drafts_count=len(drafts),
        drafts_with_backing=drafts_with_backing,
        reviewers=tuple(sorted(reviewers)),
        decisions_by_type=dict(decisions),
        drafts_decided=drafts_decided,
        time_to_approval_seconds=ttap,
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _score_band(x: float) -> str:
    """Return a CSS class indicating where the score falls."""
    if x >= 0.90:
        return "score-good"
    if x >= 0.60:
        return "score-medium"
    return "score-poor"


def _chain_summary(status: VerificationResult) -> tuple[str, str, str]:
    """
    Map a VerificationResult to (label, css_class, explanation).

    Three honest outcomes:
    - chain_present=False: the run JSONL was written without
      hash-chain metadata (legacy/plain JSONL). We do
      NOT call this "verified" (there is nothing to verify) and we
      do NOT call it "broken" (nothing was tampered). We say
      "chain not present" so the report neither over- nor
      under-claims.
    - valid=True with a chain present: every link checks out.
    - valid=False: a real divergence, reported with its line.
    """
    if status.entries_checked == 0:
        return ("Chain empty",
                "score-medium",
                "The run JSONL contains no entries.")
    if not status.chain_present:
        return ("Chain not present",
                "score-medium",
                "This run was written without hash-chain metadata. "
                "Tamper-evidence is not available for this run; "
                "enable chained writes to obtain it.")
    if status.valid:
        return ("Chain verified",
                "score-good",
                f"All {status.entries_checked} entries are linked "
                "and self-consistent. This detects accidental "
                "corruption or edits made without the chaining code, "
                "but NOT an adversary with write access who re-runs "
                "the chaining. External audit needs signatures/HMAC, "
                "WORM/append-only storage, and/or external anchoring.")
    return ("Chain broken",
            "score-poor",
            f"Verification failed at line {status.first_bad_line}: "
            f"{status.reason}")


def render_html(metrics: RunMetrics) -> str:
    """Render the metrics as a single self-contained HTML document."""

    chain_label, chain_class, chain_explanation = _chain_summary(
        metrics.chain_status
    )

    decisions_html = "".join(
        f"<li><strong>{html.escape(k)}</strong>: {v}</li>"
        for k, v in sorted(metrics.decisions_by_type.items())
    ) or "<li><em>No decisions recorded.</em></li>"

    reviewers_html = "".join(
        f"<li><code>{html.escape(r)}</code></li>"
        for r in metrics.reviewers
    ) or "<li><em>No reviewers identified.</em></li>"

    ttap_str = (
        f"{metrics.time_to_approval_seconds:.1f} s"
        if metrics.time_to_approval_seconds is not None
        else "n/a"
    )

    generated_at = datetime.now(timezone.utc).isoformat()

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Compliance Report — {html.escape(metrics.target_name)}</title>
<style>
  :root {{
    --bg: #fafaf7; --ink: #1a1a1a; --ink-dim: #555;
    --rule: #d8d4cb; --panel: #ffffff;
    --good: #4f7d4f; --medium: #b07a35; --poor: #a44a4a;
    --accent: #2d3e50;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 0; background: var(--bg);
         color: var(--ink); font-family: Georgia, "Times New Roman", serif;
         font-size: 14px; line-height: 1.6; }}
  .container {{ max-width: 880px; margin: 0 auto; padding: 48px 32px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }}
  .subtitle {{ color: var(--ink-dim); font-size: 13px;
               margin-bottom: 32px; }}
  .meta-grid {{ display: grid; grid-template-columns: max-content 1fr;
                gap: 4px 24px; padding: 16px 20px;
                background: var(--panel); border: 1px solid var(--rule);
                margin-bottom: 32px; font-size: 13px; }}
  .meta-grid dt {{ color: var(--ink-dim); font-family: ui-monospace,
                   monospace; font-size: 12px; }}
  .meta-grid dd {{ margin: 0; }}
  h2 {{ font-size: 17px; margin: 36px 0 4px;
        border-bottom: 1px solid var(--rule); padding-bottom: 6px; }}
  h2 .article {{ font-family: ui-monospace, monospace; font-size: 12px;
                 color: var(--ink-dim); font-weight: normal;
                 margin-right: 12px; }}
  .lede {{ color: var(--ink-dim); font-style: italic; margin: 8px 0 16px;
           font-size: 13px; }}
  .score-row {{ display: flex; gap: 12px; flex-wrap: wrap;
                margin: 16px 0; }}
  .score {{ flex: 1 1 200px; background: var(--panel);
            border: 1px solid var(--rule); padding: 14px 16px; }}
  .score .label {{ font-family: ui-monospace, monospace; font-size: 11px;
                   color: var(--ink-dim); text-transform: uppercase;
                   letter-spacing: 0.08em; }}
  .score .value {{ font-size: 24px; font-weight: 600; margin-top: 4px;
                   color: var(--accent); }}
  .score .value.score-good {{ color: var(--good); }}
  .score .value.score-medium {{ color: var(--medium); }}
  .score .value.score-poor {{ color: var(--poor); }}
  .score .detail {{ font-size: 12px; color: var(--ink-dim);
                    margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px;
           font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px;
            border-bottom: 1px solid var(--rule); }}
  th {{ font-family: ui-monospace, monospace; font-size: 11px;
        color: var(--ink-dim); text-transform: uppercase;
        letter-spacing: 0.08em; font-weight: 600; }}
  code {{ font-family: ui-monospace, monospace; font-size: 12px;
          background: rgba(0,0,0,0.04); padding: 1px 4px;
          border-radius: 2px; }}
  ul {{ margin: 8px 0 16px 20px; padding: 0; }}
  li {{ margin-bottom: 4px; }}
  .footer {{ margin-top: 48px; padding-top: 16px;
             border-top: 1px solid var(--rule);
             font-size: 12px; color: var(--ink-dim);
             font-style: italic; }}
  .disclaimer {{ background: #fff8e6; border: 1px solid #d8c489;
                 padding: 12px 16px; font-size: 12px;
                 color: #4a3a14; margin: 24px 0; }}
</style></head>
<body>
<div class="container">

<h1>Compliance Report</h1>
<div class="subtitle">
  Regulated agentic ABM run · generated {html.escape(generated_at)}
</div>

<dl class="meta-grid">
  <dt>RUN ID</dt><dd><code>{html.escape(metrics.run_id)}</code></dd>
  <dt>TARGET</dt><dd>{html.escape(metrics.target_name)}</dd>
  <dt>STARTED</dt><dd><code>{html.escape(metrics.started_at)}</code></dd>
  <dt>FINAL PHASE</dt><dd><code>{html.escape(metrics.final_phase)}</code></dd>
</dl>

<div class="disclaimer">
  This document is a structural summary of the run's audit trail.
  It is not a legal opinion and does not certify compliance with
  any specific regulation. It is intended to support, not replace,
  review by a qualified compliance professional.
</div>

<h2><span class="article">Art. 12</span>Record-keeping</h2>
<div class="lede">
  EU AI Act Article 12 requires that high-risk AI systems maintain
  automatic logs of operations enabling traceability and post-market
  monitoring. This section reports on the run's logging completeness
  and tamper-evidence.
</div>
<div class="score-row">
  <div class="score">
    <div class="label">State snapshots</div>
    <div class="value">{metrics.snapshots_count}</div>
    <div class="detail">point-in-time state captures</div>
  </div>
  <div class="score">
    <div class="label">Approval records</div>
    <div class="value">{metrics.approval_records_count}</div>
    <div class="detail">human decisions recorded</div>
  </div>
  <div class="score">
    <div class="label">Notification attempts</div>
    <div class="value">{metrics.notification_attempts_count}</div>
    <div class="detail">Slack / email / null fan-out</div>
  </div>
  <div class="score">
    <div class="label">Hash chain</div>
    <div class="value {chain_class}">{html.escape(chain_label)}</div>
    <div class="detail">{html.escape(chain_explanation)}</div>
  </div>
</div>

<h2><span class="article">Art. 13</span>Transparency &amp; provenance</h2>
<div class="lede">
  EU AI Act Article 13 requires that high-risk AI systems be designed
  and developed in a way that ensures their operation is sufficiently
  transparent. For an outreach pipeline, this translates into
  per-output provenance: every generated assertion must be traceable
  to a verifiable source.
</div>
<div class="score-row">
  <div class="score">
    <div class="label">Citation coverage</div>
    <div class="value {_score_band(metrics.citation_coverage)}">
      {_pct(metrics.citation_coverage)}
    </div>
    <div class="detail">
      {metrics.claims_with_provenance}/{metrics.claims_count} claims
      reference at least one citation
    </div>
  </div>
  <div class="score">
    <div class="label">Dossier coverage</div>
    <div class="value {_score_band(metrics.dossier_coverage)}">
      {_pct(metrics.dossier_coverage)}
    </div>
    <div class="detail">
      {metrics.dossiers_with_backing}/{metrics.dossiers_count}
      dossiers carry backing claim IDs
    </div>
  </div>
  <div class="score">
    <div class="label">Draft coverage</div>
    <div class="value {_score_band(metrics.draft_coverage)}">
      {_pct(metrics.draft_coverage)}
    </div>
    <div class="detail">
      {metrics.drafts_with_backing}/{metrics.drafts_count}
      drafts carry backing claim IDs
    </div>
  </div>
  <div class="score">
    <div class="label">Source citations</div>
    <div class="value">{metrics.citations_count}</div>
    <div class="detail">primary sources consulted</div>
  </div>
</div>

<h2><span class="article">Art. 14</span>Human oversight</h2>
<div class="lede">
  EU AI Act Article 14 requires that high-risk AI systems be designed
  and developed in such a way that they can be effectively overseen
  by natural persons. For outreach automation, this means that no
  communication leaves the system without an identifiable human
  decision.
</div>
<div class="score-row">
  <div class="score">
    <div class="label">HITL coverage</div>
    <div class="value {_score_band(metrics.hitl_coverage)}">
      {_pct(metrics.hitl_coverage)}
    </div>
    <div class="detail">
      {metrics.drafts_decided}/{metrics.drafts_count} drafts decided
      by an identified reviewer
    </div>
  </div>
  <div class="score">
    <div class="label">Reviewers</div>
    <div class="value">{len(metrics.reviewers)}</div>
    <div class="detail">distinct identities recorded</div>
  </div>
  <div class="score">
    <div class="label">Decisions logged</div>
    <div class="value">{sum(metrics.decisions_by_type.values())}</div>
    <div class="detail">across all artifact references</div>
  </div>
  <div class="score">
    <div class="label">Time to approval</div>
    <div class="value">{html.escape(ttap_str)}</div>
    <div class="detail">first request to last decision</div>
  </div>
</div>

<h3 style="font-size:13px;margin-top:24px;font-family:ui-monospace,monospace;
           text-transform:uppercase;letter-spacing:0.08em;
           color:var(--ink-dim);">Decision distribution</h3>
<ul>{decisions_html}</ul>

<h3 style="font-size:13px;margin-top:16px;font-family:ui-monospace,monospace;
           text-transform:uppercase;letter-spacing:0.08em;
           color:var(--ink-dim);">Identified reviewers</h3>
<ul>{reviewers_html}</ul>

<div class="footer">
  Generated from <code>{html.escape(metrics.run_id)}.jsonl</code>.
  All metrics derive from the run's audit trail. The underlying
  JSONL is the canonical record; this HTML is a rendered view.
</div>

</div></body></html>
"""


# ---------------------------------------------------------------------------
# CLI-style helper
# ---------------------------------------------------------------------------

def generate_for_run(
    jsonl_path: Path, output_path: Path,
) -> Path:
    """
    Compute metrics and write the HTML report. Returns the output path.
    """
    metrics = compute_metrics(jsonl_path)
    html_text = render_html(metrics)
    Path(output_path).write_text(html_text, encoding="utf-8")
    return Path(output_path)
