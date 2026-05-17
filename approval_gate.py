"""
approval_gate
=============

The HITL (human-in-the-loop) gate. After the outreach_drafter has
produced drafts, the supervisor calls this node, which:

1. Snapshots the current state to the run store. This makes the
   state visible to the FastAPI reviewer queue.
2. Emits a REVIEW_REQUESTED notification (Slack/email/...) so the
   reviewer knows a run is waiting. Failure to deliver is recorded
   in the audit log and does NOT block the run.
3. Waits — by polling the run store — for approval_records to
   arrive covering every draft in the run.
4. Folds the approval decisions into state["approvals"] and
   advances supervisor_phase to one of:
   - "approved"  if every draft has at least one APPROVED record
   - "rejected"  if every draft has at least one REJECTED record
                 (no APPROVED records for any draft)
   - "done"      mixed outcomes
5. Emits a REVIEW_RESOLVED notification with the outcome.

The gate is the single point at which a human decision is required
before any outbound action. Every decision is recorded with the
reviewer's identity and rationale, so the approval is auditable
after the fact rather than implicit.

Design notes
------------

- Polling, not callbacks. The JSONL run file is the source of truth;
  any reviewer process (UI, Slack bot, CLI) appends ApprovalRecords,
  the gate sees them. This survives process restarts cleanly.

- Approval granularity is per ``(run, role)``, keyed by the
  artifact ref ``artifact_ref_for_draft(run_id, role)`` ==
  ``draft:<run_id>:<role>``. A reviewer can approve
  LEGAL_COMPLIANCE, reject END_USER, and escalate ECONOMIC_BUYER
  within one run — but only at role granularity.

  KNOWN LIMITATION (not a feature): if a run produces multiple
  drafts that share a role (e.g. two LEGAL_COMPLIANCE seats), they
  collapse to one artifact ref and are ALL covered by a single
  decision. A reviewer cannot approve one same-role draft and
  reject another; the decision applies to every draft of that role.

- Timeouts are loud, not silent. If no decision arrives within
  `timeout_s`, the gate raises ApprovalTimeout with the specific
  missing artifact_refs.

- Notifications are side-effects, never blocking. A failing Slack
  webhook records a notification_attempt with outcome=FAILED in the
  audit log; the run advances. The UI is the fallback channel —
  reviewers can always discover runs via the polling queue.

Author: errer441122
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from abm_state import (
    ABMState,
    ApprovalDecision,
    ApprovalRecord,
)
from notifications import (
    MultiNotifier,
    NotificationEvent,
    NotificationKind,
    Notifier,
    NullNotifier,
    append_notification_attempt,
)
from run_store import (
    artifact_ref_for_draft,
    load_latest,
    save_snapshot,
)


class ApprovalTimeout(Exception):
    """Raised when the gate waits beyond `timeout_s` for decisions."""


def _decisions_by_ref(approvals: list[ApprovalRecord]) -> dict[str, list]:
    """Group ApprovalRecords by artifact_ref preserving order."""
    out: dict[str, list[ApprovalRecord]] = {}
    for record in approvals:
        out.setdefault(record.artifact_ref, []).append(record)
    return out


def _phase_from_decisions(
    expected_refs: set[str],
    decisions: dict[str, list[ApprovalRecord]],
) -> str:
    """
    Derive the final supervisor_phase from the per-(run, role) decisions.
    """
    if not expected_refs:
        return "done"

    most_recent: dict[str, ApprovalRecord] = {}
    for ref, records in decisions.items():
        if records:
            most_recent[ref] = records[-1]

    statuses: list[ApprovalDecision] = []
    for ref in expected_refs:
        rec = most_recent.get(ref)
        if rec is None:
            statuses.append(ApprovalDecision.PENDING)
        else:
            statuses.append(rec.decision)

    if all(s == ApprovalDecision.APPROVED for s in statuses):
        return "approved"
    if all(s == ApprovalDecision.REJECTED for s in statuses):
        return "rejected"
    return "done"


def _all_decisions_present(
    expected_refs: set[str],
    decisions: dict[str, list[ApprovalRecord]],
) -> bool:
    """All drafts have at least one non-pending decision."""
    for ref in expected_refs:
        records = decisions.get(ref, [])
        if not records:
            return False
        if records[-1].decision == ApprovalDecision.PENDING:
            return False
    return True


def _emit_and_audit(
    notifier: Notifier,
    store_dir: Path,
    run_id: str,
    event: NotificationEvent,
) -> None:
    """
    Send one notification and append the attempt(s) to the audit log.
    Never raises — a buggy notifier cannot fail a run.

    If the notifier is a MultiNotifier, its per-channel attempts are
    recorded as sub_attempts so the audit trail preserves the
    "Slack failed, email delivered" detail.
    """
    try:
        attempt = notifier.notify(event)
    except Exception as exc:
        # Notifier violated the "never raise" contract. Still audit
        # the failure so the trail is complete.
        from notifications import DeliveryOutcome, NotificationAttempt
        from run_store import now_iso
        attempt = NotificationAttempt(
            kind=event.kind,
            channel=getattr(notifier, "channel", "unknown"),
            outcome=DeliveryOutcome.FAILED,
            attempted_at=now_iso(),
            attempts_made=1,
            error=type(exc).__name__,
            run_id=event.run_id,
        )
        sub_attempts = []
    else:
        sub_attempts = (
            list(notifier.last_attempts)
            if isinstance(notifier, MultiNotifier)
            else []
        )

    try:
        append_notification_attempt(
            store_dir, run_id, attempt, sub_attempts=sub_attempts,
        )
    except Exception:
        # If the audit log itself can't be written, we have a bigger
        # problem than a missed notification. Swallowing here is
        # acceptable because the supervisor will see the run is not
        # progressing through the store regardless.
        pass


def approval_gate(
    state: ABMState,
    store_dir: Path,
    *,
    timeout_s: float = 600.0,
    poll_interval_s: float = 0.5,
    notifier: Notifier | None = None,
    review_url: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """
    LangGraph node. Snapshots the state, notifies reviewers,
    waits for human decisions on every draft, notifies on
    resolution, and returns the partial state update.

    Arguments
    ---------
    state           : the current ABMState (with outreach_drafts)
    store_dir       : directory containing the run's JSONL
    timeout_s       : seconds before raising ApprovalTimeout
    poll_interval_s : seconds between store reads
    notifier        : Notifier (defaults to NullNotifier — safe)
    review_url      : optional deep link included in notifications
    clock, sleep    : injection seams for tests

    `notifier` defaults to `NullNotifier` so cloning and running this
    lab never accidentally pages a real Slack channel.
    """
    notifier = notifier or NullNotifier()
    run_id = state["run_id"]
    drafts = state.get("outreach_drafts", [])
    target_name = state["target_account"].legal_name
    expected_refs = {
        artifact_ref_for_draft(run_id, draft.role.value)
        for draft in drafts
    }

    # Snapshot so the FastAPI reviewer queue can see the drafts.
    state_to_snapshot: ABMState = {**state,
                                   "supervisor_phase": "awaiting_approval"}
    save_snapshot(store_dir, state_to_snapshot)

    if not expected_refs:
        # No drafts to approve — phase advances to done. We still
        # emit a resolution notification so reviewers see the run
        # closed.
        _emit_and_audit(
            notifier, store_dir, run_id,
            NotificationEvent(
                kind=NotificationKind.REVIEW_RESOLVED,
                run_id=run_id,
                target_name=target_name,
                summary="run completed with no drafts to review",
                drafts_pending=0,
                review_url=review_url,
            ),
        )
        return {"supervisor_phase": "done"}

    # Notify reviewers that a run is waiting.
    _emit_and_audit(
        notifier, store_dir, run_id,
        NotificationEvent(
            kind=NotificationKind.REVIEW_REQUESTED,
            run_id=run_id,
            target_name=target_name,
            summary=f"{len(expected_refs)} draft(s) pending review",
            drafts_pending=len(expected_refs),
            review_url=review_url,
        ),
    )

    deadline = clock() + timeout_s

    while True:
        latest = load_latest(store_dir, run_id)
        decisions = _decisions_by_ref(latest.get("approvals", []))
        if _all_decisions_present(expected_refs, decisions):
            phase = _phase_from_decisions(expected_refs, decisions)
            existing_ids = {
                (r.artifact_ref, r.decided_at, r.decided_by)
                for r in state.get("approvals", [])
            }
            new_records = [
                rec for records in decisions.values()
                for rec in records
                if (rec.artifact_ref, rec.decided_at, rec.decided_by)
                not in existing_ids
            ]

            # Snapshot the terminal state so the run_store reflects
            # the final phase. Downstream tooling (compliance reports,
            # dashboards) reads from the store and would otherwise
            # only see the "awaiting_approval" snapshot we wrote on
            # entry. The in-memory return below is for the supervisor;
            # the snapshot below is for everything else.
            terminal_state: ABMState = {
                **latest,
                "supervisor_phase": phase,
                "approvals": list(latest.get("approvals", []))
                              + [rec for rec in new_records
                                 if rec not in latest.get("approvals", [])],
            }
            save_snapshot(store_dir, terminal_state)

            # Notify resolution.
            _emit_and_audit(
                notifier, store_dir, run_id,
                NotificationEvent(
                    kind=NotificationKind.REVIEW_RESOLVED,
                    run_id=run_id,
                    target_name=target_name,
                    summary=f"phase={phase}, "
                            f"{len(new_records)} new decision(s)",
                    drafts_pending=0,
                    review_url=review_url,
                ),
            )

            return {
                "approvals": new_records,
                "supervisor_phase": phase,
            }

        if clock() >= deadline:
            decisions = _decisions_by_ref(load_latest(
                store_dir, run_id).get("approvals", []))
            missing = sorted(
                ref for ref in expected_refs
                if not decisions.get(ref)
                or decisions[ref][-1].decision == ApprovalDecision.PENDING
            )
            raise ApprovalTimeout(
                f"approval_gate timed out after {timeout_s}s on run "
                f"{run_id}: {len(missing)}/{len(expected_refs)} draft(s) "
                f"still without a decision. Missing: {missing}"
            )
        sleep(poll_interval_s)
