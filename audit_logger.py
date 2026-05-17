"""
audit_logger
============

Hash-chained JSONL audit log. Sits on top of the existing run_store
JSONL and adds tamper-evidence: each entry carries the SHA-256 of
the canonicalized previous entry, so a retroactive modification
to any line breaks the chain from that point onward.

Why hash-chained?

Append-only on its own is a *social* contract: nothing in the file
system stops a process with write access from editing line 47 in
the middle of a 200-line audit. A hash chain makes such a
modification *detectable* — verify_chain() recomputes each link and
reports the first divergence in milliseconds.

Note on scope: a run JSONL written by run_store today carries no
chain metadata. That is not tampering — it is the absence of a
chain. verify_chain() distinguishes the two: it returns
chain_present=False (not valid=False) so callers can render
"chain not present" honestly instead of falsely crying "broken".

The chain extends the existing run_store JSONL by adding two
fields to every line:
- "prev_hash"  : SHA-256 of the previous entry's canonical JSON
                 (or 64 zeros for the genesis line)
- "entry_hash" : SHA-256 of the current entry's canonical JSON
                 INCLUDING prev_hash but EXCLUDING entry_hash

This means: changing any field of any entry, OR removing/reordering
any entry, OR inserting a new entry between existing ones, all
produce a chain that fails verification.

We deliberately use SHA-256 (not Merkle trees, not signatures).
SHA-256 is enough for tamper-evidence in a single-writer system;
signatures would add a key-management burden we don't need yet.

Limitation — what this does NOT defend against:

A hash chain WITHOUT external anchoring or signatures does NOT
defend against an adversary who has full write access and simply
re-runs the chaining over a rewritten file. Such an actor edits
line 47, then recomputes prev_hash/entry_hash for line 47 and
every line after it, and verify_chain() will happily report the
forged file as valid — every link is internally consistent
because the attacker rebuilt the links. This scheme is therefore
tamper-EVIDENT only against modifications by an actor or process
that does not (or cannot) recompute the chain: accidental
corruption, a truncated write, a reader, or an editor that does
not run the chaining code. It is NOT tamper-PROOF against an
adversary who controls the writer.

For an external audit you additionally need at least one of:
- an asymmetric signature, or an HMAC, computed with a key that
  is NOT available to the writer process;
- append-only / WORM storage that the writer physically cannot
  rewrite;
- external anchoring: periodically publishing the head hash to
  an independent witness (a notary, a transparency log, another
  organization) so a rewritten-and-rechained file can be caught
  by comparing against the externally recorded head.

Author: errer441122
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:  # run_store is the canonical owner of the shared writer lock.
    from run_store import _STORE_LOCK, now_iso
except ImportError:
    # run_store not importable (integrity layer exercised in
    # isolation, or a partial deployment). Fall back to a private
    # lock + timestamp so the hash chain stays usable and testable
    # on its own. When run_store IS importable we share its lock so
    # concurrent run_store/audit writers cannot interleave.
    import threading
    from datetime import datetime, timezone

    _STORE_LOCK = threading.Lock()

    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Canonical JSON serialization
# ---------------------------------------------------------------------------

def _canonical_json(obj: dict) -> str:
    """
    Deterministic JSON encoding for hashing. Sorted keys, no
    whitespace surprises, UTF-8 encoded. Two semantically-identical
    dicts MUST produce byte-identical canonical JSON.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


GENESIS_HASH = "0" * 64


# ---------------------------------------------------------------------------
# Reading: parse an existing JSONL into chain entries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChainEntry:
    """One line of the hash-chained audit log.

    `malformed=True` marks a line that could not be parsed as JSON.
    For such an entry `payload` is `{}` and `reason` carries the raw
    text plus the parse error; all other fields hold benign
    defaults. A malformed entry is, by definition, a verification
    failure: a corrupt audit file is broken, not merely "unchained".
    """
    line_no: int
    kind: str
    ts: str
    prev_hash: str
    entry_hash: str
    payload: dict
    malformed: bool = False
    reason: str = ""


def _strip_chain_fields(entry: dict) -> dict:
    """
    Strip the entry_hash from a JSON entry so we can re-canonicalize
    it for verification. The prev_hash STAYS because it is part of
    what got hashed.
    """
    return {k: v for k, v in entry.items() if k != "entry_hash"}


def read_chain(path: Path) -> list[ChainEntry]:
    """Read a JSONL file and return its chain entries."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[ChainEntry] = []
    for line_no, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            # A corrupt/truncated line is a verification failure,
            # not a crash. Record it as a malformed entry with
            # benign defaults; verify_chain() turns this into
            # valid=False (see the malformed check there).
            snippet = raw.strip()
            if len(snippet) > 80:
                snippet = snippet[:80] + "…"
            out.append(ChainEntry(
                line_no=line_no,
                kind="unknown",
                ts="",
                prev_hash=GENESIS_HASH,
                entry_hash="",
                payload={},
                malformed=True,
                reason=f"{exc.msg} (raw: {snippet!r})",
            ))
            continue
        out.append(ChainEntry(
            line_no=line_no,
            kind=entry.get("kind", "unknown"),
            ts=entry.get("ts", ""),
            prev_hash=entry.get("prev_hash", GENESIS_HASH),
            entry_hash=entry.get("entry_hash", ""),
            payload=entry,
        ))
    return out


# ---------------------------------------------------------------------------
# Writing: append a new entry with prev_hash + entry_hash
# ---------------------------------------------------------------------------

def append_chained(path: Path, entry: dict) -> str:
    """
    Append `entry` to `path` with chain metadata. Returns the new
    entry_hash so the caller can also retain it (e.g. in
    state["audit_chain_head"]).

    The `entry` dict should already contain at least "kind" and
    domain-level fields. This function adds:
        prev_hash, entry_hash, ts (if missing)

    The whole operation is guarded by the shared _STORE_LOCK so
    concurrent writers cannot interleave a partial entry.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _STORE_LOCK:
        # Find previous entry_hash by walking the existing file.
        prev_hash = GENESIS_HASH
        if path.exists():
            for raw in path.read_text(
                encoding="utf-8"
            ).splitlines()[::-1]:
                if raw.strip():
                    prev_entry = json.loads(raw)
                    prev_hash = prev_entry.get(
                        "entry_hash", GENESIS_HASH
                    )
                    break

        full = dict(entry)
        full.setdefault("ts", now_iso())
        full["prev_hash"] = prev_hash
        canonical = _canonical_json(full)
        entry_hash = _sha256_hex(canonical)
        full["entry_hash"] = entry_hash

        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(full, ensure_ascii=False, sort_keys=True)
                + "\n"
            )

    return entry_hash


# ---------------------------------------------------------------------------
# Verification: walk the chain and report the first break
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerificationResult:
    """
    Outcome of verify_chain.

    valid=True  -> every entry's prev_hash matches the previous
                   entry's entry_hash, and every entry_hash matches
                   the recomputed SHA-256 of its canonical content.
    valid=False -> the first divergence is reported in first_bad_line
                   with `reason` explaining whether prev_hash was
                   wrong or the entry's own hash was wrong.

    chain_present=False -> the file has entries but none carry
                   hash-chain metadata (e.g. a plain run_store
                   JSONL). This is NOT a verification failure:
                   `valid` stays True because there is nothing to
                   contradict, but the absence of tamper-evidence
                   is reported honestly so the compliance report
                   says "chain not present" rather than "broken".
    """
    valid: bool
    entries_checked: int
    first_bad_line: int | None = None
    reason: str = ""
    chain_present: bool = True


def verify_chain(path: Path) -> VerificationResult:
    """
    Walk the chain. Report the first inconsistency.

    Two kinds of breaks are possible:
    1. prev_hash mismatch: entry N's prev_hash != entry (N-1)'s
       entry_hash. Means an entry was inserted, removed, or
       reordered.
    2. self-hash mismatch: recomputing SHA-256 over the entry's
       canonical content (with prev_hash but without entry_hash)
       does not produce the stored entry_hash. Means a field of
       this entry was modified after writing.

    Special case: if there are entries but NONE of them carry an
    entry_hash field, the file was written without a chain. That
    is reported as valid=True, chain_present=False — not a break.

    Hard failure: if ANY line failed to parse as JSON the audit
    file is corrupt. That is checked first — before the "no chain
    metadata" and prev_hash/entry_hash checks — and reported as
    valid=False (a corrupt file is broken, full stop).
    """
    entries = read_chain(path)

    # A corrupt/truncated line short-circuits everything else: a
    # file we cannot even parse is broken, not "unchained".
    for entry in entries:
        if entry.malformed:
            short = entry.reason.split(" (raw:", 1)[0]
            return VerificationResult(
                valid=False,
                entries_checked=entry.line_no,
                first_bad_line=entry.line_no,
                reason=(
                    f"malformed JSON at line {entry.line_no}: "
                    f"{short}"
                ),
            )

    if entries and not any("entry_hash" in e.payload for e in entries):
        return VerificationResult(
            valid=True,
            entries_checked=len(entries),
            chain_present=False,
        )

    expected_prev = GENESIS_HASH
    for entry in entries:
        # Check the prev_hash link.
        if entry.prev_hash != expected_prev:
            return VerificationResult(
                valid=False,
                entries_checked=entry.line_no,
                first_bad_line=entry.line_no,
                reason=(
                    f"prev_hash mismatch at line {entry.line_no}: "
                    f"expected {expected_prev[:16]}…, got "
                    f"{entry.prev_hash[:16]}…"
                ),
            )

        # Recompute this entry's own hash.
        canonical = _canonical_json(_strip_chain_fields(entry.payload))
        recomputed = _sha256_hex(canonical)
        if recomputed != entry.entry_hash:
            return VerificationResult(
                valid=False,
                entries_checked=entry.line_no,
                first_bad_line=entry.line_no,
                reason=(
                    f"entry_hash mismatch at line {entry.line_no}: "
                    f"stored {entry.entry_hash[:16]}…, recomputed "
                    f"{recomputed[:16]}…"
                ),
            )
        expected_prev = entry.entry_hash

    return VerificationResult(
        valid=True,
        entries_checked=len(entries),
    )


# ---------------------------------------------------------------------------
# Convenience iterators for the compliance report
# ---------------------------------------------------------------------------

def iter_entries_of_kind(
    path: Path, kinds: Iterable[str]
) -> Iterable[ChainEntry]:
    """Yield chain entries whose `kind` is in the given set."""
    kinds_set = set(kinds)
    for entry in read_chain(path):
        if entry.malformed:
            continue
        if entry.kind in kinds_set:
            yield entry
