# abm/ — Multi-agent ABM Orchestration

A reference implementation of regulated, audit-ready Account-Based
Marketing automation. Four specialist agents (signal_collector,
committee_mapper, dossier_writer, outreach_drafter) coordinated by a
LangGraph supervisor, every step provenance-tracked and human-approved
before any outbound action.

## Why this module exists

Regulated B2B outreach has two hard requirements that generic agent
demos skip: every generated assertion must be traceable to a
verifiable source, and no outbound action may happen without a
recorded human decision. This module is a reference implementation
of those two properties end to end, with an audit trail designed to
support an external compliance review.

> **Context (third-party commentary, not independently verified).**
> Industry analysts (McKinsey, Stanford HAI, a16z, 2025–2026) have
> argued that the main barrier to production agentic workflows is
> governance and workflow redesign rather than model capability, and
> that human-in-the-loop validation correlates with the teams that
> get measurable value. Those figures are cited here as motivation
> only; they are *not* verified by this project and should not be
> quoted from this README as fact.

## How the agents fit together

```
TargetAccount
     │
     ▼
signal_collector        Public sources only (SEC, regulated disclosures,
     │                  tier-1 news, public LinkedIn). Content-hashed at
     │                  retrieval. No purchased intent data.
     ▼
committee_mapper        Identifies buying-committee roles from cited
     │                  signals. Every member references the claims
     │                  that justify their inclusion.
     ▼
dossier_writer          Per-role briefing. Every assertion backed by
     │                  Claim → Citation. Claims without provenance
     │                  are rejected before they reach the gate.
     ▼
outreach_drafter        Per-role draft (email / InMail). Same provenance
     │                  rule. Never sent autonomously.
     ▼
approval_gate (HITL)    Human reviewer approves, rejects, or escalates.
     │                  Decision and rationale are recorded with the
     │                  run. (The hash chain is NOT yet wired into the
     │                  write path — see "Contents of this archive" and
     │                  "What's next".)
     ▼
audit_logger            Append-only JSONL. Every state mutation, every
                        claim accepted or rejected, every approval.
                        Replayable. Exportable to the EU AI Act
                        compliance dashboard. (Hash-chain primitive
                        exists but is not on the live write path; not
                        signed — see integrity scope below.)
```

## Mapping to enterprise buyer concerns

| Concern                                       | Module answer                                |
|-----------------------------------------------|----------------------------------------------|
| Output accuracy / traceability                | Claim → Citation provenance, gate on missing |
| Effective human oversight                     | approval_gate, role-based escalation         |
| Reproducibility of what the system did        | Replayable JSONL audit, deterministic report |
| End-to-end workflow, not isolated tools       | 4-agent orchestration (see note below)       |
| EU AI Act Article 12 / 13 / 14 alignment      | Audit log, provenance, HITL gate             |
| GDPR / Article 10 data governance             | Public sources only, no intent-data purchase |

## Contents of this archive

> **This archive is a partial export.** It contains the integrity
> and reporting layer, not the full pipeline. Present files:
>
> - `audit_logger.py` — hash-chained JSONL writer + `verify_chain()`.
> - `compliance_report.py` — HTML report generator (Art. 12/13/14).
> - `approval_gate.py` — the HITL gate node.
> - `test_compliance_report.py` — full-pipeline test (needs the
>   modules below + `fastapi`; **not runnable from this archive**).
> - `test_audit_chain.py` — standalone integrity test (depends only
>   on `audit_logger` + `compliance_report` + stdlib; **runnable
>   here**).
> - `abm_compliance_report.html` — a sample generated report.
>
> The modules listed under "Full project" below (`abm_state.py`,
> `run_store.py`, `signal_collector.py`, `claim_extractor.py`,
> `committee_mapper.py`, `dossier_writer.py`, `outreach_drafter.py`,
> `hallucination_guard.py`, `approval_api.py`, `notifications.py`,
> `approval_ui.html`) and six of the seven test files are **NOT in
> this archive**. The pipeline cannot be run end to end from here;
> use `test_audit_chain.py` to exercise what is present.

## What's implemented (full project)

The full project targets a regulated agentic ABM pipeline: content
generation with provenance, hallucination guard, HITL approval,
multi-channel notifications, a hash-chain audit log (tamper-evident
only single-writer and only against an actor that does not recompute
the chain — see "What this lab is" for the precise scope), and a
compliance report deliverable mapped to EU AI Act Articles. Only
the components listed in "Contents of this archive" above are
delivered here.

Pipeline nodes:

- `abm_state.py` — LangGraph state schema.
- `signal_collector.py` — Node 1.
- `claim_extractor.py` — Pattern backend ships; LLM stub holds the
  contract.
- `committee_mapper.py` — Node 2.
- `dossier_writer.py` — Node 3.
- `outreach_drafter.py` — Node 4.
- `hallucination_guard.py` — Cross-cutting filter.
- `approval_gate.py` — Node 5 (HITL). Emits review_requested +
  review_resolved notifications, snapshots the terminal state to
  the store so downstream tooling sees the final phase.

Persistence, HTTP, notifications, audit:

- `run_store.py` — JSONL persistence (append-only, monotonic
  approval log).
- `approval_api.py` — FastAPI stateless HTTP layer.
- `approval_ui.html` — Single-page reviewer UI, zero dependencies.
- `notifications.py` — Slack webhook + SMTP + MultiNotifier with
  bounded retry and no-secret-leakage audit.
- `audit_logger.py` — Hash-chained JSONL writer (SHA-256 prev_hash
  + entry_hash). `verify_chain()` walks the chain and reports the
  first divergence with a clear reason. Detects field edits AND
  entry insertions/deletions.
- `compliance_report.py` — Generates a self-contained HTML report
  from a run's JSONL, with metrics mapped to EU AI Act Articles
  12 (record-keeping), 13 (transparency), 14 (human oversight).
  Conservative wording: the report is a structural summary, not a
  legal certification.

Tests in the full project (only the last two ship in this archive;
of those, only `test_audit_chain.py` is runnable without the
missing modules):

```bash
python test_signal_collector.py     # full project — not in archive
python test_pipeline.py             # full project — not in archive
python test_full_pipeline.py        # full project — not in archive
python test_hallucination_guard.py  # full project — not in archive
python test_approval_flow.py        # full project — not in archive
python test_notifications.py        # full project — not in archive
python test_compliance_report.py    # in archive; needs pipeline + fastapi
python test_audit_chain.py          # in archive; RUNNABLE here
```

## The compliance report

`compliance_report.py` is the deliverable a Chief Compliance Officer
can open without further context. It produces an HTML document with:

- **Run identification** — run ID, target, started_at, final phase.
- **Disclaimer** — "this is a structural summary, not a legal
  opinion". The report is meant to support, not replace,
  qualified compliance review.
- **Article 12 (Record-keeping)** — snapshot count, approval-record
  count, notification-attempt count, hash-chain status. Answers
  "can someone verify what this system did yesterday?"
- **Article 13 (Transparency)** — citation coverage (% of claims
  with backing sources), dossier coverage, draft coverage,
  source count. Answers "can every output be traced to a
  verifiable input?"
- **Article 14 (Human oversight)** — HITL coverage (% of drafts
  decided by an identified reviewer), distinct reviewer count,
  decision distribution, time-to-approval.

A representative run on the Intesa Sanpaolo fixtures produces:

- Article 12: 3 snapshots, 3 approvals, 2 notification attempts,
  chain status **"Chain not present"**. This is correct and
  honest: `run_store` writes plain JSONL today, so there is no
  hash chain to verify. It is reported as "not present", *not*
  "verified" (nothing to verify) and *not* "broken" (nothing was
  tampered). Wiring `run_store` writes through
  `audit_logger.append_chained` is residual work (see "What's
  next"); only then does the status legitimately become
  "Chain verified".
- Article 13: 100% citation coverage, 100% dossier coverage,
  100% draft coverage, 3 source citations. Note: on the bundled
  fixtures this is true *by construction* — the fixture corpus is
  written to satisfy the pattern claim extractor — so treat it as
  a wiring check, not evidence of provenance quality on real data.
- Article 14: 100% HITL coverage, 1 reviewer, all approved,
  decision recorded.

## Wiring it together (the demo runbook)

```bash
# 1. Configure notifications (optional — NullNotifier is safe-default)
export ABM_SLACK_WEBHOOK_URL="..."

# 2. Run the pipeline + the gate; in another shell serve the UI
export ABM_STORE_DIR=/tmp/abm_runs
mkdir -p $ABM_STORE_DIR
uvicorn approval_api:app &   # reviewer UI at http://localhost:8000

# 3. Run an account through the pipeline
python -c "
from pathlib import Path
from abm_state import TargetAccount, new_run
from signal_collector import FixturesAdapter, signal_collector
from claim_extractor import PatternClaimExtractor
from committee_mapper import committee_mapper
from dossier_writer import dossier_writer
from outreach_drafter import outreach_drafter
from approval_gate import approval_gate
from run_store import save_snapshot
from notifications import from_env

state = new_run(TargetAccount(legal_name='Intesa Sanpaolo', country='IT'))
state['citations'] = signal_collector(state, adapters=[FixturesAdapter(Path('fixtures'))])['citations']
state['claims'] = PatternClaimExtractor().extract(state['citations'])
state['committee'] = committee_mapper(state)['committee']
state['dossiers'] = dossier_writer(state)['dossiers']
state['outreach_drafts'] = outreach_drafter(state)['outreach_drafts']
save_snapshot(Path('/tmp/abm_runs'), state)

print('Review at http://localhost:8000/')
approval_gate(state, store_dir=Path('/tmp/abm_runs'), notifier=from_env(),
              review_url='http://localhost:8000/')
"

# 4. Generate the compliance report
python -c "
from pathlib import Path
from run_store import list_runs
from compliance_report import generate_for_run
run_id = list_runs(Path('/tmp/abm_runs'))[0]
out = generate_for_run(Path(f'/tmp/abm_runs/{run_id}.jsonl'),
                       Path('/tmp/abm_compliance_report.html'))
print(f'Report: {out}')
"
```

Open `/tmp/abm_compliance_report.html` and that is the deliverable.

## What this lab is (and is not)

The lab aims to be a reference architecture for **regulated agentic
marketing automation with an audit trail a compliance reviewer can
work from**. It combines (a) provenance-first content generation,
(b) lexical hallucination defense an audit can reproduce, (c) a HITL
approval gate, (d) a hash-chain audit primitive, (e) a compliance
report mapped to EU AI Act Articles 12/13/14. None of these are
individually novel; the intent is to have them in one place,
replayable from a single JSONL.

The audit primitive is tamper-EVIDENT only in a narrow sense: in a
single-writer setting, against an actor that does not recompute the
chain, `verify_chain()` will detect field edits and entry
insertions/deletions. It does **not** defend against an adversary
with full write access who simply re-runs the chaining over altered
entries. An external audit additionally requires at least one of:
an asymmetric signature or HMAC whose key is **not** held by the
writer; append-only / WORM storage; and/or external anchoring
(periodically publishing the head hash to an independent witness).
None of those are implemented here.

It is **not** a finished product. As shipped in this archive the
pipeline is not runnable (most modules absent) and the hash chain
is not yet wired into `run_store` (see "What's next"). The analyst
figures in "Why this module exists" are third-party commentary used
as motivation, not claims this project substantiates.

## Running the demo

In two shells:

```bash
# Shell 1: run the pipeline and trigger the gate against a real store
cd abm/
ABM_STORE_DIR=/tmp/abm_runs python -c "
from pathlib import Path
from abm_state import TargetAccount, new_run
from signal_collector import FixturesAdapter, signal_collector
from claim_extractor import PatternClaimExtractor
from committee_mapper import committee_mapper
from dossier_writer import dossier_writer
from outreach_drafter import outreach_drafter
from approval_gate import approval_gate
from run_store import save_snapshot

state = new_run(TargetAccount(legal_name='Intesa Sanpaolo', country='IT'))
state['citations'] = signal_collector(state, adapters=[FixturesAdapter(Path('fixtures'))])['citations']
state['claims'] = PatternClaimExtractor().extract(state['citations'])
state['committee'] = committee_mapper(state)['committee']
state['dossiers'] = dossier_writer(state)['dossiers']
state['outreach_drafts'] = outreach_drafter(state)['outreach_drafts']
save_snapshot(Path('/tmp/abm_runs'), state)
print('Run', state['run_id'], 'waiting for review at http://localhost:8000/')
print(approval_gate(state, store_dir=Path('/tmp/abm_runs'), timeout_s=600))
"

# Shell 2: serve the reviewer UI
cd abm/
ABM_STORE_DIR=/tmp/abm_runs uvicorn approval_api:app
```

Open http://localhost:8000/, set a reviewer ID, approve / reject /
escalate each draft. Shell 1 advances `supervisor_phase` accordingly.

## Why this matters for a regulated B2B buyer

Every outbound communication is reviewed by an identified human and
the decision is recorded with rationale. `audit_logger.py` provides
a hash-chain primitive that, once wired into the write path (see
below), makes decisions replayable and tamper-evident **in a
single-writer setting against an actor that does not recompute the
chain**. It is not a substitute for an external audit: that
additionally needs a signature or HMAC whose key is not held by the
writer, append-only / WORM storage, and/or external anchoring of the
head hash to an independent witness. Officers can review and record
decisions here; they cannot "sign off" in a cryptographic sense —
nothing is signed.

## What's next

- **Wire the hash chain into the write path.** Today `run_store`
  writes plain JSONL, so `compliance_report` correctly reports
  "Chain not present". Route `save_snapshot`,
  `append_notification_attempt` and the approval-record writes
  through `audit_logger.append_chained()` so the chain is actually
  present; only then does the report legitimately show
  "Chain verified". `append_chained()` is currently unused by the
  real pipeline.
- **Integrity hardening beyond the hash chain.** The hash chain is
  tamper-evident only single-writer and only against an actor that
  does not recompute it. For an external audit, add an asymmetric
  signature or HMAC whose key is not held by the writer, append-only
  / WORM storage, and external anchoring (periodically publishing the
  head hash to an independent witness). None of these exist yet.
- **Per-individual-draft approval granularity (known limitation).**
  Approval is described as "per-draft", but the approval identifier
  is the artifact ref `draft:<run_id>:<role>`, so granularity is
  effectively per `(run, role)`, not per individual draft. Two drafts
  that share a role (e.g. two `legal_compliance` recipients) are both
  covered by a single decision; a reviewer cannot approve one and
  reject the other. Per-individual-draft granularity would require
  changes in `run_store.artifact_ref_for_draft` / `abm_state`, which
  are **not in this archive**.
- **Make the archive self-contained or ship the full project.**
  Six of seven tests and ~11 modules are absent;
  `test_compliance_report.py` cannot run here.
- LLM wiring on the `*Backend` stubs once the guard has run against
  generated content in CI.
