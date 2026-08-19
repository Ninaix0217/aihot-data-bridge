from __future__ import annotations

import base64
import hashlib
import io
import json
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aihot_bridge.repository_data import (
    RepositoryDataError,
    report_date_for_payload,
    stage_candidate,
    validate_candidate_for_report,
    verify_repository_candidate,
)
from tests.test_snapshot import snapshot_payload


REPORT_DAY = date(2026, 8, 20)
PASS_A_GENERATED_AT = "2026-08-20T04:50:00Z"
PASS_B_GENERATED_AT = "2026-08-20T05:10:00Z"
COMMIT_SHA = "c" * 40


def candidate_payload(
    generated_at: str = PASS_A_GENERATED_AT,
    *,
    title: str = "Snapshot item",
) -> dict:
    payload = deepcopy(snapshot_payload())
    payload["generated_at"] = generated_at
    payload["window"] = {
        "type": "rolling_candidate",
        "hours": 30,
        "from": "2026-08-18T22:50:00Z",
        "to": generated_at,
    }
    payload["items"][0]["title"] = title
    return payload


def candidate_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )


def write_candidate(path: Path, payload: dict) -> bytes:
    content = candidate_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def complete_failure_payload() -> dict:
    payload = candidate_payload(PASS_B_GENERATED_AT)
    for channel in ("selected", "all", "paper"):
        payload["coverage"][channel].update(
            {"status": "failed", "source": None, "items": 0}
        )
    return payload


def test_first_success_second_failure_preserves_first_candidate(tmp_path: Path):
    source = tmp_path / "dist" / "today.json"
    data_root = tmp_path / "snapshot-data"
    first_content = write_candidate(source, candidate_payload())

    stage_candidate(source, data_root)
    write_candidate(source, complete_failure_payload())

    with pytest.raises(RepositoryDataError):
        stage_candidate(source, data_root)

    dated = data_root / "report-candidate" / "2026-08-20.json"
    assert dated.read_bytes() == first_content
    assert (data_root / "latest.json").read_bytes() == first_content


def test_second_success_replaces_first_candidate(tmp_path: Path):
    source = tmp_path / "dist" / "today.json"
    data_root = tmp_path / "snapshot-data"
    write_candidate(source, candidate_payload())
    stage_candidate(source, data_root)

    second_content = write_candidate(
        source,
        candidate_payload(PASS_B_GENERATED_AT, title="Pass B item"),
    )
    metadata = stage_candidate(source, data_root)

    dated = data_root / "report-candidate" / "2026-08-20.json"
    assert metadata.generated_at == PASS_B_GENERATED_AT
    assert dated.read_bytes() == second_content
    assert (data_root / "latest.json").read_bytes() == second_content


def test_complete_failure_publishes_no_candidate(tmp_path: Path):
    source = tmp_path / "dist" / "today.json"
    data_root = tmp_path / "snapshot-data"
    write_candidate(source, complete_failure_payload())

    with pytest.raises(RepositoryDataError):
        stage_candidate(source, data_root)
    with pytest.raises(RepositoryDataError):
        stage_candidate(source, data_root)

    assert not data_root.exists()


def test_pages_and_repository_outputs_are_byte_identical(tmp_path: Path):
    pages_candidate = tmp_path / "dist" / "report-candidate" / "2026-08-20.json"
    content = write_candidate(pages_candidate, candidate_payload())

    metadata = stage_candidate(pages_candidate, tmp_path / "snapshot-data")

    repository_candidate = (
        tmp_path / "snapshot-data" / "report-candidate" / "2026-08-20.json"
    )
    assert repository_candidate.read_bytes() == content
    assert (tmp_path / "snapshot-data" / "latest.json").read_bytes() == content
    assert metadata.sha256 == hashlib.sha256(content).hexdigest()


def test_beijing_date_is_used_across_utc_midnight_boundary():
    payload = candidate_payload("2026-08-19T16:30:00Z")

    assert report_date_for_payload(payload) == date(2026, 8, 20)


def test_window_completeness_is_required():
    payload = candidate_payload()
    payload["window"]["from"] = "2026-08-19T04:01:00Z"

    with pytest.raises(RepositoryDataError, match="does not cover"):
        validate_candidate_for_report(payload, REPORT_DAY)


def test_pass_a_is_daily_ready_even_if_pass_b_fails():
    metadata = validate_candidate_for_report(
        candidate_payload(),
        REPORT_DAY,
        now=datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc),
    )

    assert metadata.generated_at == PASS_A_GENERATED_AT


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def github_opener(content: bytes):
    blob_header = f"blob {len(content)}\0".encode("ascii")
    blob_sha = hashlib.sha1(blob_header + content).hexdigest()

    def open_url(request, **_kwargs):
        if "/git/ref/heads/" in request.full_url:
            payload = {"object": {"sha": COMMIT_SHA}}
        else:
            payload = {
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
                "sha": blob_sha,
            }
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return open_url


def test_repository_readback_verifies_commit_schema_and_hash():
    content = candidate_bytes(candidate_payload())

    result = verify_repository_candidate(
        repository="Ninaix0217/aihot-data-bridge",
        branch="snapshot-data",
        path="report-candidate/2026-08-20.json",
        token="test-token",
        expected_content=content,
        expected_generated_at=PASS_A_GENERATED_AT,
        expected_commit_sha=COMMIT_SHA,
        opener=github_opener(content),
    )

    assert result.commit_sha == COMMIT_SHA
    assert result.metadata.sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    "remote_content",
    [
        candidate_bytes(candidate_payload(title="Different repository bytes")),
        b'{"not": "a snapshot"}\n',
    ],
)
def test_publish_readback_mismatch_or_invalid_schema_fails(remote_content: bytes):
    expected_content = candidate_bytes(candidate_payload())

    with pytest.raises(RepositoryDataError):
        verify_repository_candidate(
            repository="Ninaix0217/aihot-data-bridge",
            branch="snapshot-data",
            path="report-candidate/2026-08-20.json",
            token="test-token",
            expected_content=expected_content,
            expected_generated_at=PASS_A_GENERATED_AT,
            expected_commit_sha=COMMIT_SHA,
            opener=github_opener(remote_content),
        )
