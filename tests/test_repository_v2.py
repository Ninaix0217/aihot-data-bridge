from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date
from typing import Callable, Mapping

import pytest

from aihot_bridge.candidate_v2 import validated_candidate_bytes
from aihot_bridge.candidate_versioning import CandidateDecision
from aihot_bridge.repository_v2 import (
    DATA_BRANCH,
    MAX_CAS_ATTEMPTS,
    V2_LATEST_PATH,
    GitCommitObject,
    RepositoryV2Error,
    RepositoryV2ErrorReason,
    plan_repository_update,
    publish_v2_candidate,
    v2_candidate_path,
)
from tests.v2_fixtures import complete_candidate_payload


def observation(candidate: dict, as_of: str, generated_at: str) -> dict:
    output = deepcopy(candidate)
    output["retrieval"]["as_of"] = as_of
    output["generated_at"] = generated_at
    return output


class FakeGitObjectAdapter:
    def __init__(self, files: Mapping[str, bytes] | None = None) -> None:
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, GitCommitObject] = {}
        self.refs: dict[str, str] = {}
        self.operations: list[str] = []
        self.update_calls: list[tuple[str, str, str, bool]] = []
        self.races: list[Callable[[FakeGitObjectAdapter], None]] = []
        self.always_race = False
        self.corrupt_created_blob_read = False
        self.omit_created_tree_path: str | None = None
        self.corrupt_final_latest = False
        tree_entries = {
            path: self._store_blob(content) for path, content in (files or {}).items()
        }
        tree_sha = self._store_tree(tree_entries)
        commit_sha = self._store_commit(tree_sha, ())
        self.refs[DATA_BRANCH] = commit_sha

    def read_head(self, branch: str) -> str:
        self.operations.append("read_head")
        return self.refs[branch]

    def read_commit(self, commit_sha: str) -> GitCommitObject:
        self.operations.append("read_commit")
        return self.commits[commit_sha]

    def read_tree(self, tree_sha: str) -> Mapping[str, str]:
        self.operations.append("read_tree")
        return dict(self.trees[tree_sha])

    def read_blob(self, blob_sha: str) -> bytes:
        self.operations.append("read_blob")
        content = self.blobs[blob_sha]
        if self.corrupt_created_blob_read and blob_sha.startswith("blob-"):
            self.corrupt_created_blob_read = False
            return content + b"corrupt"
        return content

    def create_blob(self, content: bytes) -> str:
        self.operations.append("create_blob")
        return self._store_blob(content, prefix="blob-")

    def create_tree(self, base_tree_sha: str, updates: Mapping[str, str]) -> str:
        self.operations.append("create_tree")
        entries = dict(self.trees[base_tree_sha])
        entries.update(updates)
        if self.omit_created_tree_path is not None:
            entries.pop(self.omit_created_tree_path, None)
        return self._store_tree(entries)

    def create_commit(self, tree_sha: str, parent_sha: str, message: str) -> str:
        self.operations.append("create_commit")
        return self._store_commit(tree_sha, (parent_sha,), message=message)

    def update_ref(
        self,
        branch: str,
        new_commit_sha: str,
        *,
        expected_old_sha: str,
        force: bool,
    ) -> bool:
        self.operations.append("update_ref")
        self.update_calls.append((branch, new_commit_sha, expected_old_sha, force))
        if self.races:
            self.races.pop(0)(self)
            return False
        if self.always_race:
            self.advance_with_files(
                {f"race/{len(self.update_calls)}.txt": b"concurrent"}
            )
            return False
        if self.refs[branch] != expected_old_sha:
            return False
        self.refs[branch] = new_commit_sha
        if self.corrupt_final_latest:
            commit = self.commits[new_commit_sha]
            bad_blob = self._store_blob(b"not the candidate")
            self.trees[commit.tree_sha][V2_LATEST_PATH] = bad_blob
        return True

    def advance_with_files(self, updates: Mapping[str, bytes]) -> str:
        head = self.refs[DATA_BRANCH]
        parent = self.commits[head]
        tree = dict(self.trees[parent.tree_sha])
        for path, content in updates.items():
            tree[path] = self._store_blob(content)
        tree_sha = self._store_tree(tree)
        commit_sha = self._store_commit(tree_sha, (head,), message="race")
        self.refs[DATA_BRANCH] = commit_sha
        return commit_sha

    def head_files(self) -> dict[str, bytes]:
        head = self.refs[DATA_BRANCH]
        tree = self.trees[self.commits[head].tree_sha]
        return {path: self.blobs[blob] for path, blob in tree.items()}

    def _store_blob(self, content: bytes, *, prefix: str = "seed-blob-") -> str:
        sha = prefix + hashlib.sha1(content).hexdigest()
        self.blobs[sha] = content
        return sha

    def _store_tree(self, entries: Mapping[str, str]) -> str:
        encoded = json.dumps(dict(sorted(entries.items())), separators=(",", ":")).encode()
        sha = "tree-" + hashlib.sha1(encoded).hexdigest()
        self.trees[sha] = dict(entries)
        return sha

    def _store_commit(
        self,
        tree_sha: str,
        parents: tuple[str, ...],
        *,
        message: str = "seed",
    ) -> str:
        encoded = f"{tree_sha}|{','.join(parents)}|{message}".encode()
        sha = "commit-" + hashlib.sha1(encoded).hexdigest()
        self.commits[sha] = GitCommitObject(tree_sha, parents)
        return sha


def test_latest_absent_accepts_dated_and_latest():
    plan = plan_repository_update(
        new_candidate=complete_candidate_payload(),
        existing_dated_candidate=None,
        existing_latest_candidate=None,
    )

    assert plan.decision is CandidateDecision.ACCEPT_NEW
    assert plan.write_dated
    assert plan.write_latest
    assert plan.target_path == "v2/report-candidate/2026-09-08.json"


def test_incomplete_new_plan_has_no_writes_but_keeps_derived_target_path():
    candidate = complete_candidate_payload()
    proof = candidate["coverage"]["selected"]["source_range"]
    proof["query_verified"] = False
    proof["state"] = "INCOMPLETE"
    candidate["coverage"]["selected"]["status"] = "partial"

    plan = plan_repository_update(
        new_candidate=candidate,
        existing_dated_candidate=None,
        existing_latest_candidate=None,
    )

    assert plan.decision is CandidateDecision.REJECT_NEW
    assert not plan.write_dated and not plan.write_latest
    assert plan.target_path == "v2/report-candidate/2026-09-08.json"


def test_new_date_advances_latest():
    new = complete_candidate_payload(report_day=date(2026, 9, 8))
    latest = complete_candidate_payload(report_day=date(2026, 9, 7))

    plan = plan_repository_update(
        new_candidate=new,
        existing_dated_candidate=None,
        existing_latest_candidate=latest,
    )

    assert plan.write_dated and plan.write_latest


def test_same_latest_date_fresher_replacement_updates_both():
    old = complete_candidate_payload()
    new = observation(old, "2026-09-08T06:00:00Z", "2026-09-08T06:01:00Z")

    plan = plan_repository_update(
        new_candidate=new,
        existing_dated_candidate=old,
        existing_latest_candidate=old,
    )

    assert plan.decision is CandidateDecision.EQUIVALENT_BUT_FRESHER
    assert plan.write_dated and plan.write_latest


def test_same_latest_date_noop_writes_nothing():
    candidate = complete_candidate_payload()

    plan = plan_repository_update(
        new_candidate=deepcopy(candidate),
        existing_dated_candidate=candidate,
        existing_latest_candidate=candidate,
    )

    assert plan.decision is CandidateDecision.IDEMPOTENT_NOOP
    assert not plan.write_dated and not plan.write_latest


def test_older_backfill_writes_dated_only_and_cannot_rewind_latest():
    backfill = complete_candidate_payload(report_day=date(2026, 9, 8))
    latest = complete_candidate_payload(report_day=date(2026, 9, 9))

    plan = plan_repository_update(
        new_candidate=backfill,
        existing_dated_candidate=None,
        existing_latest_candidate=latest,
    )

    assert plan.write_dated
    assert not plan.write_latest


def test_target_date_controls_latest_despite_observation_time():
    older_d = observation(
        complete_candidate_payload(report_day=date(2026, 9, 8)),
        "2026-09-10T05:00:00Z",
        "2026-09-10T05:01:00Z",
    )
    later_d_with_older_generation = complete_candidate_payload(
        report_day=date(2026, 9, 9)
    )

    plan = plan_repository_update(
        new_candidate=older_d,
        existing_dated_candidate=None,
        existing_latest_candidate=later_d_with_older_generation,
    )

    assert not plan.write_latest


def test_malformed_latest_fails_closed():
    with pytest.raises(RepositoryV2Error) as caught:
        plan_repository_update(
            new_candidate=complete_candidate_payload(),
            existing_dated_candidate=None,
            existing_latest_candidate={"broken": True},
        )

    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID


def test_same_date_latest_without_dated_candidate_is_invalid_repository_state():
    candidate = complete_candidate_payload()

    with pytest.raises(RepositoryV2Error) as caught:
        plan_repository_update(
            new_candidate=candidate,
            existing_dated_candidate=None,
            existing_latest_candidate=candidate,
        )

    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID


def test_same_date_latest_must_match_existing_dated_candidate():
    dated = complete_candidate_payload()
    latest = deepcopy(dated)
    latest["generated_at"] = "2026-09-08T05:02:00Z"
    new = observation(dated, "2026-09-08T06:00:00Z", "2026-09-08T06:01:00Z")

    with pytest.raises(RepositoryV2Error) as caught:
        plan_repository_update(
            new_candidate=new,
            existing_dated_candidate=dated,
            existing_latest_candidate=latest,
        )

    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID


def test_path_is_derived_and_cannot_traverse():
    assert v2_candidate_path(date(2026, 9, 8)) == (
        "v2/report-candidate/2026-09-08.json"
    )
    with pytest.raises(RepositoryV2Error):
        v2_candidate_path("../../latest.json")  # type: ignore[arg-type]


def test_publish_preserves_v1_and_unrelated_v2_files():
    existing = {
        "latest.json": b"v1 latest",
        "report-candidate/2026-09-08.json": b"v1 dated",
        "v2/report-candidate/2026-09-07.json": b"older v2 bytes",
        "notes.txt": b"unrelated",
    }
    adapter = FakeGitObjectAdapter(existing)

    result = publish_v2_candidate(adapter, complete_candidate_payload())
    files = adapter.head_files()

    assert result.branch_updated
    assert all(files[path] == content for path, content in existing.items())
    expected = validated_candidate_bytes(complete_candidate_payload())
    assert files["v2/report-candidate/2026-09-08.json"] == expected
    assert files[V2_LATEST_PATH] == expected
    assert all(not force for _, _, _, force in adapter.update_calls)


def test_incomplete_candidate_never_reads_or_updates_repository():
    candidate = complete_candidate_payload()
    proof = candidate["coverage"]["selected"]["source_range"]
    proof["query_verified"] = False
    proof["state"] = "INCOMPLETE"
    candidate["coverage"]["selected"]["status"] = "partial"
    adapter = FakeGitObjectAdapter()

    with pytest.raises(RepositoryV2Error) as caught:
        publish_v2_candidate(adapter, candidate)

    assert caught.value.reason is RepositoryV2ErrorReason.CANDIDATE_INCOMPLETE
    assert adapter.operations == []
    assert adapter.update_calls == []


def test_immutable_blob_readback_mismatch_prevents_branch_update():
    adapter = FakeGitObjectAdapter({"latest.json": b"v1"})
    original_head = adapter.refs[DATA_BRANCH]
    adapter.corrupt_created_blob_read = True

    with pytest.raises(RepositoryV2Error) as caught:
        publish_v2_candidate(adapter, complete_candidate_payload())

    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED
    assert adapter.refs[DATA_BRANCH] == original_head
    assert adapter.update_calls == []


def test_missing_intended_tree_path_prevents_branch_update():
    candidate = complete_candidate_payload()
    adapter = FakeGitObjectAdapter()
    original_head = adapter.refs[DATA_BRANCH]
    adapter.omit_created_tree_path = v2_candidate_path(date(2026, 9, 8))

    with pytest.raises(RepositoryV2Error) as caught:
        publish_v2_candidate(adapter, candidate)

    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED
    assert adapter.refs[DATA_BRANCH] == original_head
    assert adapter.update_calls == []


def test_immutable_readback_occurs_before_non_force_ref_update():
    adapter = FakeGitObjectAdapter()

    publish_v2_candidate(adapter, complete_candidate_payload())

    update_index = adapter.operations.index("update_ref")
    assert "read_blob" in adapter.operations[:update_index]
    assert "read_tree" in adapter.operations[:update_index]
    assert "read_commit" in adapter.operations[:update_index]
    assert adapter.update_calls[0][3] is False


def test_existing_same_date_latest_and_dated_must_be_byte_identical():
    candidate = complete_candidate_payload()
    dated = validated_candidate_bytes(candidate)
    latest = json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode("utf-8")
    adapter = FakeGitObjectAdapter(
        {
            v2_candidate_path(date(2026, 9, 8)): dated,
            V2_LATEST_PATH: latest,
        }
    )
    new = observation(candidate, "2026-09-08T06:00:00Z", "2026-09-08T06:01:00Z")

    with pytest.raises(RepositoryV2Error) as caught:
        publish_v2_candidate(adapter, new)

    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID
    assert adapter.update_calls == []


def test_existing_latest_without_its_matching_dated_path_is_invalid():
    latest = complete_candidate_payload(report_day=date(2026, 9, 9))
    adapter = FakeGitObjectAdapter({V2_LATEST_PATH: validated_candidate_bytes(latest)})

    with pytest.raises(RepositoryV2Error) as caught:
        publish_v2_candidate(adapter, complete_candidate_payload())

    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_STATE_INVALID
    assert adapter.update_calls == []


def test_race_re_reads_and_resolves_to_equivalent_noop():
    candidate = complete_candidate_payload()
    adapter = FakeGitObjectAdapter({"latest.json": b"v1"})

    def publish_equivalent(fake: FakeGitObjectAdapter) -> None:
        content = validated_candidate_bytes(candidate)
        fake.advance_with_files(
            {
                v2_candidate_path(date(2026, 9, 8)): content,
                V2_LATEST_PATH: content,
            }
        )

    adapter.races.append(publish_equivalent)
    result = publish_v2_candidate(adapter, candidate)

    assert result.attempts_used == 2
    assert not result.branch_updated
    assert result.plan.decision is CandidateDecision.IDEMPOTENT_NOOP
    assert len(adapter.update_calls) == 1


def test_race_re_reads_and_keeps_newer_existing_candidate():
    candidate = complete_candidate_payload()
    newer = observation(candidate, "2026-09-08T06:00:00Z", "2026-09-08T06:01:00Z")
    adapter = FakeGitObjectAdapter()

    def publish_newer(fake: FakeGitObjectAdapter) -> None:
        content = validated_candidate_bytes(newer)
        fake.advance_with_files(
            {
                v2_candidate_path(date(2026, 9, 8)): content,
                V2_LATEST_PATH: content,
            }
        )

    adapter.races.append(publish_newer)
    result = publish_v2_candidate(adapter, candidate)

    assert result.attempts_used == 2
    assert not result.branch_updated
    assert result.plan.decision is CandidateDecision.KEEP_EXISTING


def test_persistent_race_fails_after_three_attempts():
    adapter = FakeGitObjectAdapter()
    adapter.always_race = True

    with pytest.raises(RepositoryV2Error) as caught:
        publish_v2_candidate(adapter, complete_candidate_payload())

    assert MAX_CAS_ATTEMPTS == 3
    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_CONCURRENT_UPDATE
    assert len(adapter.update_calls) == 3
    assert all(not force for _, _, _, force in adapter.update_calls)


def test_historical_backfill_commit_omits_latest():
    latest = complete_candidate_payload(report_day=date(2026, 9, 9))
    latest_bytes = validated_candidate_bytes(latest)
    adapter = FakeGitObjectAdapter(
        {
            V2_LATEST_PATH: latest_bytes,
            v2_candidate_path(date(2026, 9, 9)): latest_bytes,
        }
    )
    backfill = complete_candidate_payload(report_day=date(2026, 9, 8))

    publish_v2_candidate(adapter, backfill)
    files = adapter.head_files()

    assert files[V2_LATEST_PATH] == latest_bytes
    assert files[v2_candidate_path(date(2026, 9, 8))] == validated_candidate_bytes(
        backfill
    )


def test_dated_and_latest_update_are_atomic_and_byte_identical():
    adapter = FakeGitObjectAdapter()
    candidate = complete_candidate_payload()

    result = publish_v2_candidate(adapter, candidate)
    commit = adapter.commits[result.commit_sha]
    tree = adapter.trees[commit.tree_sha]

    assert tree[v2_candidate_path(date(2026, 9, 8))] == tree[V2_LATEST_PATH]
    assert adapter.blobs[tree[V2_LATEST_PATH]] == validated_candidate_bytes(candidate)


def test_final_latest_mismatch_is_readback_failure():
    adapter = FakeGitObjectAdapter()
    adapter.corrupt_final_latest = True

    with pytest.raises(RepositoryV2Error) as caught:
        publish_v2_candidate(adapter, complete_candidate_payload())

    assert caught.value.reason is RepositoryV2ErrorReason.REPOSITORY_READBACK_FAILED


def test_same_as_of_conflict_fails_without_branch_update():
    existing = complete_candidate_payload()
    new = deepcopy(existing)
    new["items"][0]["title"] = "Divergent title"
    content = validated_candidate_bytes(existing)
    adapter = FakeGitObjectAdapter(
        {
            v2_candidate_path(date(2026, 9, 8)): content,
            V2_LATEST_PATH: content,
        }
    )
    original_head = adapter.refs[DATA_BRANCH]

    with pytest.raises(RepositoryV2Error) as caught:
        publish_v2_candidate(adapter, new)

    assert caught.value.reason is RepositoryV2ErrorReason.SAME_AS_OF_CONFLICT
    assert adapter.refs[DATA_BRANCH] == original_head
    assert adapter.update_calls == []
