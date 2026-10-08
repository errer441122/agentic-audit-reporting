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
- **Deterministic.** Same JSONL in produces the same HTML out;
  pass ``generated_at`` to render_html()/generate_for_run() to pin
  the only time-dependent field. This is how the report becomes
  diffable across runs.
- **Conservative.** Metrics show what they show; we do NOT
  interpret them as compliance certifications. The HTML
  explicitly says "this is a structural summary, not a legal
  opinion." That sentence is the difference between a useful
  audit aid and a document that overpromises.

Author: errer441122
"""

from __future__ import annotations

import html
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
class DraftRow:
    """One outreach draft: its provenance and the decision taken on it."""

    role: str
    claims: int        # backing claim IDs on the draft
    citations: int     # distinct sources behind those claims
    decision: str      # "" when no approval record matches the draft
    decided_by: str
    decided_at: str


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

    # Evidence: one row per outreach draft (Articles 13 and 14).
    draft_rows: tuple[DraftRow, ...] = ()

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

    # Malformed lines are skipped here, not raised: verify_chain() below
    # reports them, so a corrupt file renders "Chain broken" instead of
    # crashing the report it is supposed to appear in.
    raw_entries = [e.payload for e in read_chain(jsonl_path) if not e.malformed]

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

    # Per-draft evidence: backing claims, the distinct sources behind
    # them, and the latest decision recorded for the draft's role.
    cites_by_claim = {c.get("id"): c.get("citation_ids") or [] for c in claims}
    decision_by_ref: dict[str, dict] = {}
    for ap_entry in approvals:
        rec = ap_entry.get("record", {})
        if rec.get("artifact_ref"):
            decision_by_ref[rec["artifact_ref"]] = rec
    draft_rows = []
    for d in drafts:
        claim_ids = d.get("backing_claim_ids") or []
        rec = decision_by_ref.get(
            artifact_ref_for_draft(latest_state["run_id"], d.get("role")), {}
        )
        draft_rows.append(DraftRow(
            role=str(d.get("role") or "?"),
            claims=len(claim_ids),
            citations=len({c for k in claim_ids for c in cites_by_claim.get(k, [])}),
            decision=str(rec.get("decision") or ""),
            decided_by=str(rec.get("decided_by") or ""),
            decided_at=str(rec.get("decided_at") or ""),
        ))

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
        draft_rows=tuple(draft_rows),
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
    where = (f" at line {status.first_bad_line}"
             if status.first_bad_line is not None else "")
    return ("Chain broken",
            "score-poor",
            f"Verification failed{where}: {status.reason}")


def _fmt_ts(ts: str) -> str:
    """'2026-05-16T10:42:10+00:00' -> '16 May 2026, 10:42 UTC'."""
    dt = _parse_ts(ts)
    if dt is None:
        return ts or "—"
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return f"{dt.day} {dt:%b %Y, %H:%M} UTC"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def _role(role: str) -> str:
    """'legal_compliance' -> 'Legal compliance'."""
    return role.replace("_", " ").capitalize() if role else "?"


_CSS = """
:root {
  --bg: #f3f4f6; --panel: #ffffff; --ink: #111827; --ink-2: #4b5563; --muted: #6b7280;
  --line: #e5e7eb; --track: #eef0f3; --accent: #1f3a5f;
  --good: #15803d; --good-bg: #ecfdf3; --medium: #b45309; --medium-bg: #fff7e6;
  --poor: #b91c1c; --poor-bg: #fef2f2;
  --mono: ui-monospace, "SFMono-Regular", Consolas, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14.5px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.page { max-width: 920px; margin: 32px auto; background: var(--panel); border: 1px solid var(--line);
  border-radius: 14px; padding: 40px 44px; box-shadow: 0 1px 3px rgba(17,24,39,.06); }
code { font-family: var(--mono); font-size: 12.5px; background: var(--track); padding: 1px 5px;
  border-radius: 4px; overflow-wrap: anywhere; }
.eyebrow, .k { font-size: 11.5px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
h1 { font-size: 26px; line-height: 1.2; margin: 6px 0 18px; letter-spacing: -0.01em; }
h1 span { color: var(--ink-2); font-weight: 500; }
.meta { display: grid; grid-template-columns: minmax(0, 1.7fr) repeat(3, minmax(0, 1fr)); gap: 12px 20px; margin: 0;
  padding: 14px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.meta dt { font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
.meta dd { margin: 2px 0 0; }
.pill { display: inline-block; font-size: 12px; font-weight: 600; padding: 1px 9px; border-radius: 999px;
  background: var(--track); color: var(--ink-2); }
.pill.approved { background: var(--good-bg); color: var(--good); }
.pill.rejected { background: var(--poor-bg); color: var(--poor); }
.pill.escalated, .pill.pending { background: var(--medium-bg); color: var(--medium); }
.verdict { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 22px 0 18px; }
.v { border-radius: 12px; padding: 14px 16px; border: 1px solid var(--line); border-top-width: 4px; }
.v b { display: block; font-size: 22px; line-height: 1.25; margin: 4px 0 2px; }
.v small { color: var(--ink-2); font-size: 13px; }
.score-good { border-top-color: var(--good); } .score-good b { color: var(--good); }
.score-medium { border-top-color: var(--medium); } .score-medium b { color: var(--medium); }
.score-poor { border-top-color: var(--poor); } .score-poor b { color: var(--poor); }
.disclaimer { background: var(--medium-bg); border: 1px solid #f3d9a4; color: #5b4310; border-radius: 10px;
  padding: 11px 14px; font-size: 13px; margin: 0 0 8px; }
section { margin-top: 30px; }
h2 { font-size: 18px; margin: 0 0 4px; display: flex; align-items: baseline; gap: 10px; }
h2 .art { font-family: var(--mono); font-size: 12px; font-weight: 600; color: var(--accent);
  background: #e8eef6; padding: 2px 8px; border-radius: 6px; }
h3 { font-size: 13px; margin: 18px 0 8px; color: var(--ink-2); }
.lede { color: var(--ink-2); margin: 4px 0 14px; font-size: 13.5px; max-width: 78ch; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.tile { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.tile b { display: block; font-size: 22px; margin: 2px 0; font-variant-numeric: tabular-nums; }
.tile small { color: var(--muted); font-size: 12.5px; }
.chain { margin-top: 10px; border: 1px solid var(--line); border-left-width: 4px; border-radius: 10px;
  padding: 14px 16px; display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 16px; }
.chain.score-good { border-left-color: var(--good); } .chain.score-medium { border-left-color: var(--medium); }
.chain.score-poor { border-left-color: var(--poor); }
.chain b { display: block; font-size: 18px; margin: 2px 0 4px; }
.chain p, .chain small { margin: 0; color: var(--ink-2); font-size: 13px; }
.chain .head code { display: block; margin: 4px 0 6px; padding: 6px 8px; }
.meters { display: grid; gap: 12px; }
.meter { display: grid; grid-template-columns: 170px minmax(0, 1fr) 64px; gap: 12px; align-items: center; }
.meter .bar { height: 10px; border-radius: 5px; background: var(--track); overflow: hidden; }
.meter .bar i { display: block; height: 100%; border-radius: 5px; background: var(--good); }
.meter.score-medium .bar i { background: var(--medium); } .meter.score-poor .bar i { background: var(--poor); }
.meter b { text-align: right; font-variant-numeric: tabular-nums; }
.meter small { grid-column: 2 / 4; margin-top: -8px; color: var(--muted); font-size: 12.5px; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; margin-top: 6px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
thead th { font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; font-weight: 600; }
td.n { text-align: right; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
.two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
ul.plain { margin: 0; padding-left: 18px; }
.limit { font-size: 12.5px; color: var(--muted); margin: 14px 0 0; }
footer { margin-top: 34px; padding-top: 14px; border-top: 1px solid var(--line); font-size: 12.5px; color: var(--muted); }
@media (max-width: 720px) {
  .page { margin: 0; border-radius: 0; border: 0; padding: 24px 16px; }
  .meta { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .verdict, .two, .chain { grid-template-columns: 1fr; }
  .meter { grid-template-columns: minmax(0, 1fr) 56px; } .meter .bar { grid-column: 1 / 3; grid-row: 2; }
  .meter small { grid-column: 1 / 3; margin-top: 0; }
}
@media print {
  @page { size: A4; margin: 14mm; }
  body { background: #fff; } .page { margin: 0; border: 0; box-shadow: none; padding: 0; max-width: none; }
  section, .verdict, .chain, table { break-inside: avoid; }
}
"""


def _tile(label: str, value: str, detail: str) -> str:
    return (f'<div class="tile"><span class="k">{html.escape(label)}</span>'
            f'<b>{html.escape(value)}</b><small>{html.escape(detail)}</small></div>')


def _meter(label: str, share: float, detail: str) -> str:
    return (f'<div class="meter {_score_band(share)}"><span>{html.escape(label)}</span>'
            f'<span class="bar"><i style="width:{max(share, 0.0) * 100:.1f}%"></i></span>'
            f'<b>{_pct(share)}</b><small>{html.escape(detail)}</small></div>')


def render_html(metrics: RunMetrics, generated_at: str | None = None) -> str:
    """Render the metrics as a single self-contained HTML document.

    Pass `generated_at` (ISO-8601) for byte-identical output across runs;
    it defaults to the current UTC time.
    """
    m = metrics
    esc = html.escape
    chain_label, chain_class, chain_explanation = _chain_summary(m.chain_status)
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    head = m.chain_status.head_hash

    head_html = (
        f'<div class="head"><span class="k">Head hash (SHA-256)</span><code>{esc(head)}</code>'
        '<small>Record this value outside the system. A log that is later '
        'rewritten or truncated will no longer end at this hash.</small></div>'
        if head and m.chain_status.valid else ""
    )

    draft_rows = "".join(
        f'<tr><td>{esc(_role(d.role))}</td><td class="n">{d.claims}</td>'
        f'<td class="n">{d.citations}</td>'
        f'<td><span class="pill {esc(d.decision or "pending")}">{esc(d.decision or "pending")}</span></td>'
        f'<td>{f"<code>{esc(d.decided_by)}</code>" if d.decided_by else "—"}</td>'
        f'<td>{esc(_fmt_ts(d.decided_at)) if d.decided_at else "—"}</td></tr>'
        for d in m.draft_rows
    ) or '<tr><td colspan="6"><em>No outreach drafts in this run.</em></td></tr>'

    decisions_html = "".join(
        f'<li><span class="pill {esc(k)}">{esc(k)} × {v}</span></li>'
        for k, v in sorted(m.decisions_by_type.items())
    ) or "<li><em>No decisions recorded.</em></li>"

    reviewers_html = "".join(
        f"<li><code>{esc(r)}</code></li>" for r in m.reviewers
    ) or "<li><em>No reviewers identified.</em></li>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Compliance Report — {esc(m.target_name)}</title>
<style>{_CSS}</style></head>
<body>
<div class="page">

<header>
<div class="eyebrow">Regulated agentic ABM run · EU AI Act audit-trail report</div>
<h1>Compliance Report <span>— {esc(m.target_name)}</span></h1>
<dl class="meta">
  <div><dt>Run ID</dt><dd><code>{esc(m.run_id)}</code></dd></div>
  <div><dt>Started</dt><dd>{esc(_fmt_ts(m.started_at))}</dd></div>
  <div><dt>Final phase</dt><dd><span class="pill {esc(m.final_phase)}">{esc(m.final_phase)}</span></dd></div>
  <div><dt>Generated</dt><dd>{esc(_fmt_ts(generated_at))}</dd></div>
</dl>
</header>

<section class="verdict" aria-label="Summary">
  <div class="v {chain_class}"><span class="k">Art. 12 · Record-keeping</span><b>{esc(chain_label)}</b>
    <small>{m.chain_status.entries_checked} log entries checked</small></div>
  <div class="v {_score_band(m.citation_coverage)}"><span class="k">Art. 13 · Transparency</span><b>{_pct(m.citation_coverage)}</b>
    <small>of claims cite at least one source</small></div>
  <div class="v {_score_band(m.hitl_coverage)}"><span class="k">Art. 14 · Human oversight</span><b>{_pct(m.hitl_coverage)}</b>
    <small>of drafts decided by an identified reviewer</small></div>
</section>

<p class="disclaimer">This document is a structural summary of the run's audit trail.
It is not a legal opinion and does not certify compliance with any specific regulation.
It is intended to support, not replace, review by a qualified compliance professional.</p>

<section>
<h2><span class="art">Art. 12</span>Record-keeping</h2>
<p class="lede">EU AI Act Article 12 requires that high-risk AI systems maintain automatic logs of
operations enabling traceability and post-market monitoring. This section reports on the run's logging
completeness and tamper-evidence.</p>
<div class="tiles">
  {_tile("State snapshots", str(m.snapshots_count), "point-in-time state captures")}
  {_tile("Approval records", str(m.approval_records_count), "human decisions recorded")}
  {_tile("Notification attempts", str(m.notification_attempts_count), "Slack / email / null fan-out")}
</div>
<div class="chain {chain_class}">
  <div><span class="k">Hash chain</span><b>{esc(chain_label)}</b><p>{esc(chain_explanation)}</p></div>
  {head_html}
</div>
</section>

<section>
<h2><span class="art">Art. 13</span>Transparency &amp; provenance</h2>
<p class="lede">EU AI Act Article 13 requires that high-risk AI systems be designed and developed in a way
that ensures their operation is sufficiently transparent. For an outreach pipeline, this translates into
per-output provenance: every generated assertion must be traceable to a verifiable source.</p>
<div class="meters">
  {_meter("Citation coverage", m.citation_coverage, f"{m.claims_with_provenance}/{m.claims_count} claims reference at least one citation")}
  {_meter("Dossier coverage", m.dossier_coverage, f"{m.dossiers_with_backing}/{m.dossiers_count} dossiers carry backing claim IDs")}
  {_meter("Draft coverage", m.draft_coverage, f"{m.drafts_with_backing}/{m.drafts_count} drafts carry backing claim IDs")}
</div>
<h3>Per-draft provenance and decision · {m.citations_count} primary sources consulted</h3>
<div class="scroll"><table>
<thead><tr><th>Draft</th><th class="n">Backing claims</th><th class="n">Sources</th><th>Decision</th><th>Reviewer</th><th>Decided</th></tr></thead>
<tbody>{draft_rows}</tbody>
</table></div>
</section>

<section>
<h2><span class="art">Art. 14</span>Human oversight</h2>
<p class="lede">EU AI Act Article 14 requires that high-risk AI systems be designed and developed in such a
way that they can be effectively overseen by natural persons. For outreach automation, this means that no
communication leaves the system without an identifiable human decision.</p>
<div class="tiles">
  {_tile("HITL coverage", _pct(m.hitl_coverage), f"{m.drafts_decided}/{m.drafts_count} drafts decided by an identified reviewer")}
  {_tile("Reviewers", str(len(m.reviewers)), "distinct identities recorded")}
  {_tile("Decisions logged", str(sum(m.decisions_by_type.values())), "across all artifact references")}
  {_tile("Time to approval", _fmt_duration(m.time_to_approval_seconds), "first request to last decision")}
</div>
<div class="two">
  <div><h3>Decision distribution</h3><ul class="chips">{decisions_html}</ul></div>
  <div><h3>Identified reviewers</h3><ul class="plain">{reviewers_html}</ul></div>
</div>
<p class="limit">Coverage counts every draft <em>role</em> that received a decision: two drafts for the
same role share one approval key, so 100% means every role was decided, not every recipient
individually reviewed.</p>
</section>

<footer>Generated from <code>{esc(m.run_id)}.jsonl</code>. All metrics derive from the run's audit trail.
The underlying JSONL is the canonical record; this HTML is a rendered view.</footer>

</div></body></html>
"""


# ---------------------------------------------------------------------------
# CLI-style helper
# ---------------------------------------------------------------------------

def generate_for_run(
    jsonl_path: Path, output_path: Path, generated_at: str | None = None,
) -> Path:
    """
    Compute metrics and write the HTML report. Returns the output path.
    """
    metrics = compute_metrics(jsonl_path)
    html_text = render_html(metrics, generated_at=generated_at)
    Path(output_path).write_text(html_text, encoding="utf-8", newline="\n")
    return Path(output_path)
