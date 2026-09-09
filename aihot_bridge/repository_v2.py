from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Mapping, Protocol

from .candidate_v2 import (
    CandidateCompletenessState,
    CandidateV2Metadata,
    evaluate_candidate_completeness,
    validated_candidate_bytes,
)
from .candidate_versioning import (
    CandidateComparison,
    CandidateDecision,
    CandidateDecisionReason,
    artifact_sha256,
    compare_candidates,
    semantic_candidate_hash,
)


DATA_BRANCH = "snapshot-data"
V2_LATEST_PATH = "v2/latest.json"
MAX_CAS_ATTEMPTS = 3


class RepositoryV2ErrorReason(str, Enum):
    EXISTING_CANDIDATE_INVALID = "EXISTING_CANDIDATE_INVALID"
    CANDIDATE_INCOMPLETE = "CANDIDATE_INCOMPLETE"
    STALE_ATTEMPT = "STALE_ATTEMPT"
    SAME_AS_OF_CONFLICT = "SAME_AS_OF_CONFLICT"
    CANDIDATE_CONTRACT_MISMATCH = "CANDIDATE_CONTRACT_MISMATCH"
    REPOSITORY_STATE_INVALID = "REPOSITORY_STATE_INVALID"
    REPOSITORY_CONCURRENT_UPDATE = "REPOSITORY_CONCURRENT_UPDATE"
    REPOSITORY_PUBLISH_FAILED = "REPOSITORY_PUBLISH_FAILED"
    REPOSITORY_READBACK_FAILED = "REPOSITORY_READBACK_FAILED"


class RepositoryV2Error(RuntimeError):
    def __init__(self, reason: RepositoryV2ErrorReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


@dataclass(frozen=True)
class GitCommitObject:
    tree_sha: str
    parents: tuple[str, ...]


class GitObjectAdapter(Protocol):
    """Minimal Git object/ref surface needed by the Phase D publisher."""

    def read_head(self, branch: str) -> str: ...

    def read_commit(self, commit_sha: str) -> GitCommitObject: ...

    def read_tree(self, tree_sha: str) -> Mapping[str, str]: ...

    def read_blob(self, blob_sha: str) -> bytes: ...

    def create_blob(self, content: bytes) -> str: ...

    def create_tree(self, base_tree_sha: str, updates: Mapping[str, str]) -> str: ...

    def create_commit(self, tree_sha: str, parent_sha: str, message: str) -> str: ...

    def update_ref(
        self,
        branch: str,
        new_commit_sha: str,
        *,
        expected_old_sha: str,
        force: bool,
    ) -> bool: ...


@dataclass(frozen=True)
class RepositoryUpdatePlan:
    comparison: CandidateComparison
    decision: CandidateDecision
    write_dated: bool
    write_latest: bool
    target_path: str | None
    latest_path: str
    reason: CandidateDecisionReason
    expected_content_hash: str | None
    expected_artifact_sha256: str | None


@dataclass(frozen=True)
class RepositoryPublishResult:
    plan: RepositoryUpdatePlan
    attempts_used: int
    original_head: str
    final_head: str
    commit_sha: str | None
    branch_updated: bool


@dataclass(frozen=True)
class _RepositoryCandidateObject:
    payload: dict[str, Any]
    content: bytes


def v2_candidate_path(target_report_date: date) -> str:
    if type(target_report_date) is not date:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            "target_report_date must be a date",
        )
    return f"v2/report-candidate/{target_report_date.isoformat()}.json"


def should_update_latest(
    existing_latest: Any | None,
    accepted_candidate: Any,
    *,
    accepted_decision: CandidateDecision,
) -> bool:
    if accepted_decision not in {
        CandidateDecision.ACCEPT_NEW,
        CandidateDecision.REPLACE_WITH_NEW,
        CandidateDecision.EQUIVALENT_BUT_FRESHER,
    }:
        return False
    accepted = evaluate_candidate_completeness(accepted_candidate)
    if accepted.state is not CandidateCompletenessState.VALID_COMPLETE:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.CANDIDATE_INCOMPLETE,
            "accepted candidate is not VALID_COMPLETE",
        )
    assert accepted.metadata is not None
    if existing_latest is None:
        return True
    latest = evaluate_candidate_completeness(existing_latest)
    if latest.state is not CandidateCompletenessState.VALID_COMPLETE:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            "v2/latest.json is malformed or not VALID_COMPLETE",
        )
    assert latest.metadata is not None
    return accepted.metadata.target_report_date >= latest.metadata.target_report_date


def plan_repository_update(
    *,
    new_candidate: Any,
    existing_dated_candidate: Any | None,
    existing_latest_candidate: Any | None,
) -> RepositoryUpdatePlan:
    comparison = compare_candidates(existing_dated_candidate, new_candidate)
    if comparison.reason is CandidateDecisionReason.EXISTING_CANDIDATE_INVALID:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.EXISTING_CANDIDATE_INVALID,
            "existing dated V2 candidate is malformed or incomplete",
        )
    new_evaluation = evaluate_candidate_completeness(new_candidate)
    metadata = new_evaluation.metadata
    _validate_latest_consistency(
        existing_dated_candidate=existing_dated_candidate,
        existing_latest_candidate=existing_latest_candidate,
        target_metadata=metadata,
    )
    target_path = (
        v2_candidate_path(metadata.target_report_date)
        if metadata is not None
        else _safe_candidate_path(new_candidate)
    )
    write_dated = comparison.accepts_new
    write_latest = False
    if write_dated:
        write_latest = should_update_latest(
            existing_latest_candidate,
            new_candidate,
            accepted_decision=comparison.decision,
        )
    elif existing_latest_candidate is not None:
        latest = evaluate_candidate_completeness(existing_latest_candidate)
        if latest.state is not CandidateCompletenessState.VALID_COMPLETE:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
                "v2/latest.json is malformed or not VALID_COMPLETE",
            )
    return RepositoryUpdatePlan(
        comparison=comparison,
        decision=comparison.decision,
        write_dated=write_dated,
        write_latest=write_latest,
        target_path=target_path,
        latest_path=V2_LATEST_PATH,
        reason=comparison.reason,
        expected_content_hash=comparison.new_content_hash,
        expected_artifact_sha256=comparison.new_artifact_sha256,
    )


def publish_v2_candidate(
    adapter: GitObjectAdapter,
    new_candidate: dict[str, Any],
    *,
    max_cas_attempts: int = MAX_CAS_ATTEMPTS,
) -> RepositoryPublishResult:
    if max_cas_attempts < 1:
        raise ValueError("max_cas_attempts must be at least 1")
    new_evaluation = evaluate_candidate_completeness(new_candidate)
    if new_evaluation.state is not CandidateCompletenessState.VALID_COMPLETE:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.CANDIDATE_INCOMPLETE,
            "new candidate is not VALID_COMPLETE",
        )
    assert new_evaluation.metadata is not None
    target_path = v2_candidate_path(new_evaluation.metadata.target_report_date)
    expected_bytes = _candidate_bytes(new_candidate)
    expected_artifact_hash = artifact_sha256(new_candidate)
    expected_content_hash = semantic_candidate_hash(new_candidate)
    first_head: str | None = None

    for attempt in range(1, max_cas_attempts + 1):
        try:
            head = adapter.read_head(DATA_BRANCH)
            first_head = first_head or head
            head_commit = adapter.read_commit(head)
            base_tree = dict(adapter.read_tree(head_commit.tree_sha))
            existing_dated_object = _read_optional_candidate_object(
                adapter, base_tree, target_path
            )
            existing_latest_object = _read_optional_candidate_object(
                adapter, base_tree, V2_LATEST_PATH
            )
            _verify_existing_latest_invariant(
                adapter,
                base_tree,
                existing_latest_object,
            )
            plan = plan_repository_update(
                new_candidate=new_candidate,
                existing_dated_candidate=(
                    None
                    if existing_dated_object is None
                    else existing_dated_object.payload
                ),
                existing_latest_candidate=(
                    None
                    if existing_latest_object is None
                    else existing_latest_object.payload
                ),
            )
            if plan.decision is CandidateDecision.CONFLICT:
                reason = (
                    RepositoryV2ErrorReason.SAME_AS_OF_CONFLICT
                    if plan.reason is CandidateDecisionReason.SAME_AS_OF_CONFLICT
                    else RepositoryV2ErrorReason.CANDIDATE_CONTRACT_MISMATCH
                )
                raise RepositoryV2Error(reason, plan.reason.value)
            if not plan.write_dated:
                return RepositoryPublishResult(
                    plan=plan,
                    attempts_used=attempt,
                    original_head=first_head,
                    final_head=head,
                    commit_sha=None,
                    branch_updated=False,
                )

            allowed_paths = {target_path, V2_LATEST_PATH}
            updates: dict[str, str] = {}
            candidate_blob = adapter.create_blob(expected_bytes)
            if adapter.read_blob(candidate_blob) != expected_bytes:
                raise RepositoryV2Error(
                    RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                    "created candidate blob readback does not match expected bytes",
                )
            updates[target_path] = candidate_blob
            if plan.write_latest:
                updates[V2_LATEST_PATH] = candidate_blob
            if not set(updates).issubset(allowed_paths):
                raise RepositoryV2Error(
                    RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
                    "publisher attempted to update a path outside the V2 namespace",
                )

            new_tree_sha = adapter.create_tree(head_commit.tree_sha, updates)
            new_tree = dict(adapter.read_tree(new_tree_sha))
            _verify_tree_preservation(base_tree, new_tree, updates)
            commit_sha = adapter.create_commit(
                new_tree_sha,
                head,
                f"Publish V2 candidate {new_evaluation.metadata.target_report_date}",
            )
            _verify_immutable_commit(
                adapter,
                commit_sha=commit_sha,
                expected_parent=head,
                expected_tree_sha=new_tree_sha,
                expected_paths=updates,
                expected_bytes=expected_bytes,
                expected_artifact_hash=expected_artifact_hash,
                expected_content_hash=expected_content_hash,
            )

            updated = adapter.update_ref(
                DATA_BRANCH,
                commit_sha,
                expected_old_sha=head,
                force=False,
            )
            if not updated:
                continue
            _verify_final_branch(
                adapter,
                commit_sha=commit_sha,
                target_path=target_path,
                write_latest=plan.write_latest,
                expected_bytes=expected_bytes,
                expected_artifact_hash=expected_artifact_hash,
                expected_content_hash=expected_content_hash,
            )
            return RepositoryPublishResult(
                plan=plan,
                attempts_used=attempt,
                original_head=first_head,
                final_head=commit_sha,
                commit_sha=commit_sha,
                branch_updated=True,
            )
        except RepositoryV2Error:
            raise
        except Exception as exc:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_PUBLISH_FAILED,
                f"repository adapter operation failed: {exc}",
            ) from exc

    raise RepositoryV2Error(
        RepositoryV2ErrorReason.REPOSITORY_CONCURRENT_UPDATE,
        f"snapshot-data changed during all {max_cas_attempts} CAS attempts",
    )


def _candidate_bytes(candidate: dict[str, Any]) -> bytes:
    return validated_candidate_bytes(candidate)


def _safe_candidate_path(candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return None
    value = candidate.get("target_report_date")
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        report_day = date.fromisoformat(value)
    except ValueError:
        return None
    if report_day.isoformat() != value:
        return None
    return v2_candidate_path(report_day)


def _validate_latest_consistency(
    *,
    existing_dated_candidate: Any | None,
    existing_latest_candidate: Any | None,
    target_metadata: CandidateV2Metadata | None,
) -> None:
    if existing_latest_candidate is None:
        return
    latest = evaluate_candidate_completeness(existing_latest_candidate)
    if latest.state is not CandidateCompletenessState.VALID_COMPLETE:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            "v2/latest.json is malformed or not VALID_COMPLETE",
        )
    assert latest.metadata is not None
    if target_metadata is None or (
        latest.metadata.target_report_date != target_metadata.target_report_date
    ):
        return
    if existing_dated_candidate is None:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            "v2/latest.json targets D but the matching dated candidate is missing",
        )
    dated = evaluate_candidate_completeness(existing_dated_candidate)
    if dated.state is not CandidateCompletenessState.VALID_COMPLETE:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.EXISTING_CANDIDATE_INVALID,
            "matching dated candidate is malformed or not VALID_COMPLETE",
        )
    if artifact_sha256(existing_latest_candidate) != artifact_sha256(
        existing_dated_candidate
    ):
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            "v2/latest.json is not byte-equivalent to its matching dated candidate",
        )


def _read_optional_candidate_object(
    adapter: GitObjectAdapter,
    tree: Mapping[str, str],
    path: str,
) -> _RepositoryCandidateObject | None:
    blob_sha = tree.get(path)
    if blob_sha is None:
        return None
    content = adapter.read_blob(blob_sha)
    payload = _decode_candidate_bytes(content, path=path)
    return _RepositoryCandidateObject(payload, content)


def _read_optional_candidate(
    adapter: GitObjectAdapter,
    tree: Mapping[str, str],
    path: str,
) -> dict[str, Any] | None:
    candidate = _read_optional_candidate_object(adapter, tree, path)
    return None if candidate is None else candidate.payload


def _decode_candidate_bytes(content: bytes, *, path: str) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            f"{path} is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            f"{path} JSON root is not an object",
        )
    return payload


def _verify_existing_latest_invariant(
    adapter: GitObjectAdapter,
    tree: Mapping[str, str],
    latest: _RepositoryCandidateObject | None,
) -> None:
    if latest is None:
        return
    latest_evaluation = evaluate_candidate_completeness(latest.payload)
    if latest_evaluation.state is not CandidateCompletenessState.VALID_COMPLETE:
        return
    assert latest_evaluation.metadata is not None
    matching_path = v2_candidate_path(latest_evaluation.metadata.target_report_date)
    matching_blob = tree.get(matching_path)
    if matching_blob is None or adapter.read_blob(matching_blob) != latest.content:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID,
            "existing v2/latest.json is not byte-identical to its dated candidate",
        )


def _verify_tree_preservation(
    base_tree: Mapping[str, str],
    new_tree: Mapping[str, str],
    updates: Mapping[str, str],
) -> None:
    expected_paths = set(base_tree) | set(updates)
    if set(new_tree) != expected_paths:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
            "created tree added or removed an unexpected repository path",
        )
    for path, blob_sha in base_tree.items():
        if path not in updates and new_tree.get(path) != blob_sha:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                f"unrelated repository path changed: {path}",
            )
    for path, blob_sha in updates.items():
        if new_tree.get(path) != blob_sha:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                f"commit tree is missing the intended blob at {path}",
            )


def _verify_immutable_commit(
    adapter: GitObjectAdapter,
    *,
    commit_sha: str,
    expected_parent: str,
    expected_tree_sha: str,
    expected_paths: Mapping[str, str],
    expected_bytes: bytes,
    expected_artifact_hash: str,
    expected_content_hash: str,
) -> None:
    commit = adapter.read_commit(commit_sha)
    if commit.tree_sha != expected_tree_sha or commit.parents != (expected_parent,):
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
            "immutable commit tree or parent readback mismatch",
        )
    tree = adapter.read_tree(commit.tree_sha)
    observed_content: bytes | None = None
    for path, expected_blob in expected_paths.items():
        if tree.get(path) != expected_blob:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                f"immutable commit does not contain intended path {path}",
            )
        observed_content = adapter.read_blob(expected_blob)
        if observed_content != expected_bytes:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                f"immutable blob bytes mismatch at {path}",
            )
    assert observed_content is not None
    payload = _decode_candidate_bytes(observed_content, path="immutable candidate")
    if (
        artifact_sha256(payload) != expected_artifact_hash
        or semantic_candidate_hash(payload) != expected_content_hash
    ):
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
            "immutable candidate hashes do not match the intended artifact",
        )


def _verify_final_branch(
    adapter: GitObjectAdapter,
    *,
    commit_sha: str,
    target_path: str,
    write_latest: bool,
    expected_bytes: bytes,
    expected_artifact_hash: str,
    expected_content_hash: str,
) -> None:
    if adapter.read_head(DATA_BRANCH) != commit_sha:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
            "snapshot-data ref did not reach the intended commit",
        )
    commit = adapter.read_commit(commit_sha)
    tree = adapter.read_tree(commit.tree_sha)
    candidate_blob = tree.get(target_path)
    if candidate_blob is None or adapter.read_blob(candidate_blob) != expected_bytes:
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
            "final dated candidate bytes do not match the intended artifact",
        )
    payload = _read_optional_candidate(adapter, tree, target_path)
    assert payload is not None
    if (
        artifact_sha256(payload) != expected_artifact_hash
        or semantic_candidate_hash(payload) != expected_content_hash
    ):
        raise RepositoryV2Error(
            RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
            "final dated candidate hashes do not match the intended artifact",
        )
    if write_latest:
        latest_blob = tree.get(V2_LATEST_PATH)
        if latest_blob is None or adapter.read_blob(latest_blob) != expected_bytes:
            raise RepositoryV2Error(
                RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED,
                "final latest bytes are not identical to the dated candidate",
            )
