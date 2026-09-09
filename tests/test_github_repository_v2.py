from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from aihot_bridge.github_repository_v2 import (
    GitHubAdapterError,
    GitHubAdapterErrorReason,
    GitHubGitDataAdapter,
)
from aihot_bridge.repository_v2 import publish_v2_candidate
from tests.v2_fixtures import complete_candidate_payload


REPOSITORY = "Ninaix0217/aihot-data-bridge"


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def adapter_for(handler, *, attempts: int = 3) -> tuple[GitHubGitDataAdapter, httpx.Client]:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    adapter = GitHubGitDataAdapter(
        client,
        repository=REPOSITORY,
        transport_attempts=attempts,
        transport_retry_delay_seconds=0,
        sleep=lambda _: None,
    )
    return adapter, client


class FakeGitHubApi:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.blobs = {git_blob_sha(content): content for content in files.values()}
        entries = {path: git_blob_sha(content) for path, content in files.items()}
        tree_sha = self._tree_sha(entries)
        self.trees = {tree_sha: entries}
        commit_sha = "1" * 40
        self.commits = {commit_sha: (tree_sha, ())}
        self.head = commit_sha
        self.operations: list[tuple[str, str]] = []
        self.patch_bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.operations.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if request.method == "GET" and "/git/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            tree_sha, parents = self.commits[sha]
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "tree": {"sha": tree_sha},
                    "parents": [{"sha": parent} for parent in parents],
                },
            )
        if request.method == "GET" and "/git/trees/" in path:
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "truncated": False,
                    "tree": [
                        {"path": name, "type": "blob", "sha": blob}
                        for name, blob in sorted(self.trees[sha].items())
                    ],
                },
            )
        if request.method == "GET" and "/git/blobs/" in path:
            sha = path.rsplit("/", 1)[-1]
            content = self.blobs[sha]
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "encoding": "base64",
                    "size": len(content),
                    "content": base64.b64encode(content).decode(),
                },
            )
        body = json.loads(request.read())
        if request.method == "POST" and path.endswith("/git/blobs"):
            content = base64.b64decode(body["content"])
            sha = git_blob_sha(content)
            self.blobs[sha] = content
            return httpx.Response(201, json={"sha": sha})
        if request.method == "POST" and path.endswith("/git/trees"):
            entries = dict(self.trees[body["base_tree"]])
            entries.update({entry["path"]: entry["sha"] for entry in body["tree"]})
            sha = self._tree_sha(entries)
            self.trees[sha] = entries
            return httpx.Response(201, json={"sha": sha})
        if request.method == "POST" and path.endswith("/git/commits"):
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            sha = hashlib.sha1(encoded).hexdigest()
            self.commits[sha] = (body["tree"], tuple(body["parents"]))
            return httpx.Response(201, json={"sha": sha})
        if request.method == "PATCH" and "/git/refs/heads/" in path:
            self.patch_bodies.append(body)
            self.head = body["sha"]
            return httpx.Response(200, json={"object": {"sha": self.head}})
        raise AssertionError(f"unexpected request {request.method} {path}")

    def head_files(self) -> dict[str, bytes]:
        tree_sha, _ = self.commits[self.head]
        return {path: self.blobs[blob] for path, blob in self.trees[tree_sha].items()}

    @staticmethod
    def _tree_sha(entries: dict[str, str]) -> str:
        encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha1(encoded).hexdigest()


def test_reads_snapshot_data_ref_with_required_headers():
    expected = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/git/ref/heads/snapshot-data")
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        return httpx.Response(200, json={"object": {"sha": expected}})

    adapter, client = adapter_for(handler)
    try:
        assert adapter.read_head("snapshot-data") == expected
    finally:
        client.close()


def test_missing_contents_path_404_is_expected_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/contents/v2/report-candidate/2026-09-10.json" in request.url.path
        return httpx.Response(404, json={"message": "Not Found"})

    adapter, client = adapter_for(handler)
    try:
        assert (
            adapter.read_path_at_commit(
                "v2/report-candidate/2026-09-10.json", "a" * 40
            )
            is None
        )
    finally:
        client.close()


def test_reads_commit_tree_and_base64_blob_with_shape_and_sha_validation():
    commit_sha = "a" * 40
    tree_sha = "b" * 40
    parent_sha = "c" * 40
    content = b"candidate bytes"
    blob_sha = git_blob_sha(content)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/commits/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "sha": commit_sha,
                    "tree": {"sha": tree_sha},
                    "parents": [{"sha": parent_sha}],
                },
            )
        if "/git/trees/" in request.url.path:
            assert request.url.params["recursive"] == "1"
            return httpx.Response(
                200,
                json={
                    "sha": tree_sha,
                    "truncated": False,
                    "tree": [
                        {"path": "v2", "type": "tree", "sha": "d" * 40},
                        {
                            "path": "v2/latest.json",
                            "type": "blob",
                            "sha": blob_sha,
                        },
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "sha": blob_sha,
                "encoding": "base64",
                "size": len(content),
                "content": base64.b64encode(content).decode() + "\n",
            },
        )

    adapter, client = adapter_for(handler)
    try:
        commit = adapter.read_commit(commit_sha)
        tree = adapter.read_tree(tree_sha)
        blob = adapter.read_blob(blob_sha)
    finally:
        client.close()

    assert commit.tree_sha == tree_sha
    assert commit.parents == (parent_sha,)
    assert tree == {"v2/latest.json": blob_sha}
    assert blob == content


def test_creates_blob_tree_and_commit_using_base_tree_and_v2_paths_only():
    content = b"candidate"
    blob_sha = git_blob_sha(content)
    base_tree = "a" * 40
    tree_sha = "b" * 40
    parent_sha = "c" * 40
    commit_sha = "d" * 40
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        payload = __import__("json").loads(body) if body else {}
        seen.append((request.url.path, payload))
        if request.url.path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": blob_sha})
        if request.url.path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": tree_sha})
        return httpx.Response(201, json={"sha": commit_sha})

    adapter, client = adapter_for(handler)
    try:
        assert adapter.create_blob(content) == blob_sha
        assert (
            adapter.create_tree(
                base_tree,
                {
                    "v2/latest.json": blob_sha,
                    "v2/report-candidate/2026-09-10.json": blob_sha,
                },
            )
            == tree_sha
        )
        assert adapter.create_commit(tree_sha, parent_sha, "message") == commit_sha
    finally:
        client.close()

    assert seen[1][1]["base_tree"] == base_tree
    assert {entry["path"] for entry in seen[1][1]["tree"]} == {
        "v2/latest.json",
        "v2/report-candidate/2026-09-10.json",
    }
    assert seen[2][1]["parents"] == [parent_sha]


def test_successful_ref_update_is_explicitly_non_force():
    old_sha = "a" * 40
    new_sha = "b" * 40
    patch_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"object": {"sha": old_sha}})
        patch_payload.update(__import__("json").loads(request.read()))
        return httpx.Response(200, json={"object": {"sha": new_sha}})

    adapter, client = adapter_for(handler)
    try:
        assert adapter.update_ref(
            "snapshot-data",
            new_sha,
            expected_old_sha=old_sha,
            force=False,
        )
    finally:
        client.close()

    assert patch_payload == {"sha": new_sha, "force": False}


@pytest.mark.parametrize("status", [409, 422])
def test_ref_conflict_maps_to_cas_retry_only_when_head_changed(status: int):
    old_sha = "a" * 40
    advanced_sha = "c" * 40
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"object": {"sha": old_sha}})
        if calls == 2:
            return httpx.Response(status, json={"message": "conflict"})
        return httpx.Response(200, json={"object": {"sha": advanced_sha}})

    adapter, client = adapter_for(handler)
    try:
        assert not adapter.update_ref(
            "snapshot-data",
            "b" * 40,
            expected_old_sha=old_sha,
            force=False,
        )
    finally:
        client.close()


def test_422_without_head_change_is_invalid_request_not_transient():
    old_sha = "a" * 40
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.method == "PATCH":
            return httpx.Response(422, json={"message": "invalid"})
        return httpx.Response(200, json={"object": {"sha": old_sha}})

    adapter, client = adapter_for(handler)
    try:
        with pytest.raises(GitHubAdapterError) as caught:
            adapter.update_ref(
                "snapshot-data",
                "b" * 40,
                expected_old_sha=old_sha,
                force=False,
            )
    finally:
        client.close()

    assert caught.value.reason is GitHubAdapterErrorReason.INVALID_REQUEST
    assert calls == 3


def test_5xx_retries_are_bounded_and_separate_from_cas():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json={"object": {"sha": "a" * 40}})

    adapter, client = adapter_for(handler, attempts=3)
    try:
        assert adapter.read_head("snapshot-data") == "a" * 40
    finally:
        client.close()
    assert calls == 3


def test_network_timeout_retries_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json={"object": {"sha": "a" * 40}})

    adapter, client = adapter_for(handler, attempts=2)
    try:
        assert adapter.read_head("snapshot-data") == "a" * 40
    finally:
        client.close()
    assert calls == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_not_retried(status: int):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"message": "denied"})

    adapter, client = adapter_for(handler)
    try:
        with pytest.raises(GitHubAdapterError) as caught:
            adapter.read_head("snapshot-data")
    finally:
        client.close()
    assert caught.value.reason is GitHubAdapterErrorReason.AUTH_FAILED
    assert calls == 1


def test_malformed_json_and_shape_fail_closed():
    responses = iter(
        [
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"object": {"sha": "short"}}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    adapter, client = adapter_for(handler)
    try:
        with pytest.raises(GitHubAdapterError) as malformed:
            adapter.read_head("snapshot-data")
        with pytest.raises(GitHubAdapterError) as shape:
            adapter.read_head("snapshot-data")
    finally:
        client.close()
    assert malformed.value.reason is GitHubAdapterErrorReason.MALFORMED_RESPONSE
    assert shape.value.reason is GitHubAdapterErrorReason.UNEXPECTED_RESPONSE


def test_recursive_tree_truncation_fails_closed():
    tree_sha = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sha": tree_sha, "truncated": True, "tree": []},
        )

    adapter, client = adapter_for(handler)
    try:
        with pytest.raises(GitHubAdapterError) as caught:
            adapter.read_tree(tree_sha)
    finally:
        client.close()
    assert caught.value.reason is GitHubAdapterErrorReason.TRUNCATED_TREE


def test_namespace_and_path_traversal_are_rejected_before_http():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be called")

    adapter, client = adapter_for(handler)
    try:
        with pytest.raises(GitHubAdapterError):
            adapter.read_head("main")
        with pytest.raises(GitHubAdapterError):
            adapter.read_path_at_commit("v2/../latest.json", "a" * 40)
        with pytest.raises(GitHubAdapterError):
            adapter.create_tree("a" * 40, {"latest.json": "b" * 40})
        with pytest.raises(GitHubAdapterError):
            adapter.update_ref(
                "snapshot-data",
                "b" * 40,
                expected_old_sha="a" * 40,
                force=True,
            )
    finally:
        client.close()


def test_real_adapter_and_publisher_contract_preserve_v1_over_mock_http():
    v1_files = {
        "latest.json": b"v1 latest",
        "report-candidate/2026-09-10.json": b"v1 dated",
        "unrelated.txt": b"preserve me",
    }
    api = FakeGitHubApi(v1_files)
    adapter, client = adapter_for(api)
    try:
        result = publish_v2_candidate(adapter, complete_candidate_payload())
    finally:
        client.close()

    files = api.head_files()
    assert result.branch_updated
    assert all(files[path] == content for path, content in v1_files.items())
    assert set(files) - set(v1_files) == {
        "v2/latest.json",
        "v2/report-candidate/2026-09-08.json",
    }
    assert files["v2/latest.json"] == files["v2/report-candidate/2026-09-08.json"]
    assert api.patch_bodies == [{"sha": result.commit_sha, "force": False}]
    patch_index = next(
        index for index, operation in enumerate(api.operations) if operation[0] == "PATCH"
    )
    assert any(
        method == "GET" and f"/git/commits/{result.commit_sha}" in path
        for method, path in api.operations[:patch_index]
    )
