from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .candidate_v2 import (
    ALL_CHANNELS,
    CandidateCompletenessState,
    CandidateV2Metadata,
    evaluate_candidate_completeness,
    validate_candidate_v2,
    validated_candidate_bytes,
)


class CandidateDecision(str, Enum):
    ACCEPT_NEW = "ACCEPT_NEW"
    REPLACE_WITH_NEW = "REPLACE_WITH_NEW"
    EQUIVALENT_BUT_FRESHER = "EQUIVALENT_BUT_FRESHER"
    IDEMPOTENT_NOOP = "IDEMPOTENT_NOOP"
    KEEP_EXISTING = "KEEP_EXISTING"
    REJECT_NEW = "REJECT_NEW"
    CONFLICT = "CONFLICT"


class CandidateDecisionReason(str, Enum):
    NO_EXISTING_CANDIDATE = "NO_EXISTING_CANDIDATE"
    CANDIDATE_INCOMPLETE = "CANDIDATE_INCOMPLETE"
    EXISTING_CANDIDATE_INVALID = "EXISTING_CANDIDATE_INVALID"
    STALE_ATTEMPT = "STALE_ATTEMPT"
    SAME_AS_OF_EQUIVALENT = "SAME_AS_OF_EQUIVALENT"
    SAME_AS_OF_CONFLICT = "SAME_AS_OF_CONFLICT"
    EQUIVALENT_CONTENT_NEWER = "EQUIVALENT_CONTENT_NEWER"
    NEWER_COMPLETE_CANDIDATE = "NEWER_COMPLETE_CANDIDATE"
    CANDIDATE_CONTRACT_MISMATCH = "CANDIDATE_CONTRACT_MISMATCH"


@dataclass(frozen=True)
class CandidateComparison:
    decision: CandidateDecision
    reason: CandidateDecisionReason
    target_report_date: str | None
    existing_retrieval_as_of: str | None
    new_retrieval_as_of: str | None
    existing_content_hash: str | None
    new_content_hash: str | None
    existing_artifact_sha256: str | None
    new_artifact_sha256: str | None

    @property
    def accepts_new(self) -> bool:
        return self.decision in {
            CandidateDecision.ACCEPT_NEW,
            CandidateDecision.REPLACE_WITH_NEW,
            CandidateDecision.EQUIVALENT_BUT_FRESHER,
        }


def candidate_contract_compatible(old: Any, new: Any) -> bool:
    """Compare business/query contracts without using runtime observability."""
    return _contract_projection(old) == _contract_projection(new)


def semantic_candidate_hash(candidate: Any) -> str:
    """Hash business semantics while excluding observation-time noise."""
    validate_candidate_v2(candidate)
    projection = _semantic_projection(candidate)
    return hashlib.sha256(_canonical_json(projection)).hexdigest()


def artifact_sha256(candidate: Any) -> str:
    """Hash the complete deterministic V2 JSON artifact bytes."""
    return hashlib.sha256(validated_candidate_bytes(candidate)).hexdigest()


def compare_candidates(existing: Any | None, new: Any) -> CandidateComparison:
    existing_evaluation = (
        None if existing is None else evaluate_candidate_completeness(existing)
    )
    if (
        existing_evaluation is not None
        and existing_evaluation.state is not CandidateCompletenessState.VALID_COMPLETE
    ):
        return _comparison(
            CandidateDecision.CONFLICT,
            CandidateDecisionReason.EXISTING_CANDIDATE_INVALID,
            existing_metadata=None,
            new_metadata=None,
        )

    new_evaluation = evaluate_candidate_completeness(new)
    if new_evaluation.state is not CandidateCompletenessState.VALID_COMPLETE:
        if existing is not None and not candidate_contract_compatible(existing, new):
            return _comparison(
                CandidateDecision.CONFLICT,
                CandidateDecisionReason.CANDIDATE_CONTRACT_MISMATCH,
                existing_metadata=existing_evaluation.metadata,
                new_metadata=None,
            )
        return _comparison(
            CandidateDecision.REJECT_NEW
            if existing is None
            else CandidateDecision.KEEP_EXISTING,
            CandidateDecisionReason.CANDIDATE_INCOMPLETE,
            existing_metadata=(
                None if existing_evaluation is None else existing_evaluation.metadata
            ),
            new_metadata=None,
        )

    new_metadata = new_evaluation.metadata
    assert new_metadata is not None
    new_content_hash = semantic_candidate_hash(new)
    new_artifact_hash = artifact_sha256(new)
    if existing is None:
        return _comparison(
            CandidateDecision.ACCEPT_NEW,
            CandidateDecisionReason.NO_EXISTING_CANDIDATE,
            existing_metadata=None,
            new_metadata=new_metadata,
            new_content_hash=new_content_hash,
            new_artifact_hash=new_artifact_hash,
        )

    existing_metadata = existing_evaluation.metadata
    assert existing_metadata is not None
    if not candidate_contract_compatible(existing, new):
        return _comparison(
            CandidateDecision.CONFLICT,
            CandidateDecisionReason.CANDIDATE_CONTRACT_MISMATCH,
            existing_metadata=existing_metadata,
            new_metadata=new_metadata,
        )

    existing_content_hash = semantic_candidate_hash(existing)
    existing_artifact_hash = artifact_sha256(existing)
    if new_metadata.retrieval_as_of < existing_metadata.retrieval_as_of:
        decision = CandidateDecision.KEEP_EXISTING
        reason = CandidateDecisionReason.STALE_ATTEMPT
    elif new_metadata.retrieval_as_of == existing_metadata.retrieval_as_of:
        if new_content_hash == existing_content_hash:
            decision = CandidateDecision.IDEMPOTENT_NOOP
            reason = CandidateDecisionReason.SAME_AS_OF_EQUIVALENT
        else:
            decision = CandidateDecision.CONFLICT
            reason = CandidateDecisionReason.SAME_AS_OF_CONFLICT
    elif new_content_hash == existing_content_hash:
        decision = CandidateDecision.EQUIVALENT_BUT_FRESHER
        reason = CandidateDecisionReason.EQUIVALENT_CONTENT_NEWER
    else:
        decision = CandidateDecision.REPLACE_WITH_NEW
        reason = CandidateDecisionReason.NEWER_COMPLETE_CANDIDATE

    return _comparison(
        decision,
        reason,
        existing_metadata=existing_metadata,
        new_metadata=new_metadata,
        existing_content_hash=existing_content_hash,
        new_content_hash=new_content_hash,
        existing_artifact_hash=existing_artifact_hash,
        new_artifact_hash=new_artifact_hash,
    )


def _comparison(
    decision: CandidateDecision,
    reason: CandidateDecisionReason,
    *,
    existing_metadata: CandidateV2Metadata | None,
    new_metadata: CandidateV2Metadata | None,
    existing_content_hash: str | None = None,
    new_content_hash: str | None = None,
    existing_artifact_hash: str | None = None,
    new_artifact_hash: str | None = None,
) -> CandidateComparison:
    target = new_metadata or existing_metadata
    return CandidateComparison(
        decision=decision,
        reason=reason,
        target_report_date=(
            None if target is None else target.target_report_date.isoformat()
        ),
        existing_retrieval_as_of=(
            None
            if existing_metadata is None
            else existing_metadata.retrieval_as_of.isoformat()
        ),
        new_retrieval_as_of=(
            None if new_metadata is None else new_metadata.retrieval_as_of.isoformat()
        ),
        existing_content_hash=existing_content_hash,
        new_content_hash=new_content_hash,
        existing_artifact_sha256=existing_artifact_hash,
        new_artifact_sha256=new_artifact_hash,
    )


def _contract_projection(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {"malformed": True}
    report_window = candidate.get("report_window")
    retrieval = candidate.get("retrieval")
    return {
        "schema_version": candidate.get("schema_version"),
        "producer_contract_version": candidate.get("producer_contract_version"),
        "target_report_date": candidate.get("target_report_date"),
        "report_window": (
            {
                "from": report_window.get("from"),
                "to": report_window.get("to"),
                "timezone": report_window.get("timezone"),
                "bounds": report_window.get("bounds"),
            }
            if isinstance(report_window, dict)
            else report_window
        ),
        "retrieval": (
            {
                "upstream_window": retrieval.get("upstream_window"),
                "by": retrieval.get("by"),
                "ordering": retrieval.get("ordering"),
                "source_range_contract_version": retrieval.get(
                    "source_range_contract_version"
                ),
                "primary_queries": retrieval.get("primary_queries"),
            }
            if isinstance(retrieval, dict)
            else retrieval
        ),
    }


def _semantic_projection(candidate: dict[str, Any]) -> dict[str, Any]:
    coverage = candidate["coverage"]
    stable_coverage = {}
    for channel in ALL_CHANNELS:
        entry = coverage[channel]
        stable_coverage[channel] = {
            "status": entry.get("status"),
            "source": entry.get("source"),
            "items": entry.get("items"),
            "source_range_state": entry["source_range"].get("state"),
        }
    items = []
    for value in candidate["items"]:
        item = deepcopy(value)
        channels = item.get("source_channels")
        if isinstance(channels, list):
            item["source_channels"] = sorted(set(channels))
        items.append(item)
    items.sort(key=lambda item: _canonical_json(item))
    return {
        "contract": _contract_projection(candidate),
        "coverage": stable_coverage,
        "summary": deepcopy(candidate["summary"]),
        "items": items,
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
