from __future__ import annotations

import base64
import hashlib
import re
import time
from collections.abc import Callable
from enum import Enum
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from .repository_v2 import DATA_BRANCH, GitCommitObject, GitObjectAdapter, V2_LATEST_PATH


API_VERSION = "2022-11-28"
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DATED_PATH_PATTERN = re.compile(r"^v2/report-candidate/\d{4}-\d{2}-\d{2}\.json$")


class GitHubAdapterErrorReason(str, Enum):
    AUTH_FAILED = "GITHUB_AUTH_FAILED"
    NOT_FOUND = "GITHUB_NOT_FOUND"
    REF_CONFLICT = "GITHUB_REF_CONFLICT"
    INVALID_REQUEST = "GITHUB_INVALID_REQUEST"
    SERVER_ERROR = "GITHUB_SERVER_ERROR"
    NETWORK_ERROR = "GITHUB_NETWORK_ERROR"
    MALFORMED_RESPONSE = "GITHUB_MALFORMED_RESPONSE"
    UNEXPECTED_RESPONSE = "GITHUB_UNEXPECTED_RESPONSE"
    TRUNCATED_TREE = "GITHUB_TRUNCATED_TREE"
    NAMESPACE_VIOLATION = "GITHUB_NAMESPACE_VIOLATION"


class GitHubAdapterError(RuntimeError):
    def __init__(
        self,
        reason: GitHubAdapterErrorReason,
        detail: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"{reason.value}: {detail}")


class GitHubGitDataAdapter(GitObjectAdapter):
    """Strict synchronous adapter for GitHub's Git Data API."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        repository: str,
        transport_attempts: int = 3,
        transport_retry_delay_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not _REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("repository must use owner/name syntax")
        if transport_attempts < 1:
            raise ValueError("transport_attempts must be at least 1")
        self._client = client
        self._repository = repository
        self._transport_attempts = transport_attempts
        self._retry_delay = transport_retry_delay_seconds
        self._sleep = sleep

    def read_head(self, branch: str) -> str:
        payload = self._request_json(
            "GET",
            f"/repos/{self._repository}/git/ref/heads/{_encoded_branch(branch)}",
        )
        obj = _dict(payload.get("object"), "ref.object")
        return _sha(obj.get("sha"), "ref.object.sha")

    def read_commit(self, commit_sha: str) -> GitCommitObject:
        sha = _sha(commit_sha, "commit_sha")
        payload = self._request_json(
            "GET", f"/repos/{self._repository}/git/commits/{sha}"
        )
        response_sha = _sha(payload.get("sha"), "commit.sha")
        if response_sha != sha:
            _unexpected("commit response SHA does not match requested SHA")
        tree = _dict(payload.get("tree"), "commit.tree")
        parents_value = payload.get("parents")
        if not isinstance(parents_value, list):
            _unexpected("commit.parents must be a list")
        parents = tuple(
            _sha(_dict(parent, "commit.parent").get("sha"), "commit.parent.sha")
            for parent in parents_value
        )
        return GitCommitObject(
            tree_sha=_sha(tree.get("sha"), "commit.tree.sha"),
            parents=parents,
        )

    def read_tree(self, tree_sha: str) -> Mapping[str, str]:
        sha = _sha(tree_sha, "tree_sha")
        payload = self._request_json(
            "GET",
            f"/repos/{self._repository}/git/trees/{sha}",
            params={"recursive": "1"},
        )
        if payload.get("truncated") is True:
            raise GitHubAdapterError(
                GitHubAdapterErrorReason.TRUNCATED_TREE,
                "recursive Git tree response is truncated",
            )
        if payload.get("truncated") is not False:
            _unexpected("tree.truncated must be false")
        response_sha = _sha(payload.get("sha"), "tree.sha")
        if response_sha != sha:
            _unexpected("tree response SHA does not match requested SHA")
        entries = payload.get("tree")
        if not isinstance(entries, list):
            _unexpected("tree.tree must be a list")
        blobs: dict[str, str] = {}
        for raw_entry in entries:
            entry = _dict(raw_entry, "tree entry")
            entry_type = entry.get("type")
            if entry_type not in {"blob", "tree", "commit"}:
                _unexpected("tree entry has an unsupported type")
            if entry_type == "tree":
                continue
            path = _safe_repository_path(entry.get("path"))
            if path in blobs:
                _unexpected(f"tree contains duplicate blob path {path}")
            blobs[path] = _sha(entry.get("sha"), f"tree[{path}].sha")
        return blobs

    def read_blob(self, blob_sha: str) -> bytes:
        sha = _sha(blob_sha, "blob_sha")
        payload = self._request_json(
            "GET", f"/repos/{self._repository}/git/blobs/{sha}"
        )
        response_sha = _sha(payload.get("sha"), "blob.sha")
        if response_sha != sha:
            _unexpected("blob response SHA does not match requested SHA")
        if payload.get("encoding") != "base64":
            _unexpected("blob.encoding must be base64")
        content = payload.get("content")
        if not isinstance(content, str):
            _unexpected("blob.content must be a base64 string")
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
        except ValueError as exc:
            raise GitHubAdapterError(
                GitHubAdapterErrorReason.MALFORMED_RESPONSE,
                "blob.content is not valid base64",
            ) from exc
        size = payload.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size != len(decoded):
            _unexpected("blob.size does not match decoded content")
        if _git_blob_sha(decoded) != sha:
            _unexpected("blob bytes do not match the Git blob SHA")
        return decoded

    def read_path_at_commit(self, path: str, commit_sha: str) -> bytes | None:
        safe_path = _safe_repository_path(path)
        sha = _sha(commit_sha, "commit_sha")
        encoded_path = "/".join(quote(part, safe="") for part in safe_path.split("/"))
        payload = self._request_json(
            "GET",
            f"/repos/{self._repository}/contents/{encoded_path}",
            params={"ref": sha},
            allow_not_found=True,
        )
        if payload is None:
            return None
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            _unexpected("Contents response must describe a base64 file")
        blob_sha = _sha(payload.get("sha"), "contents.sha")
        content = payload.get("content")
        if not isinstance(content, str):
            _unexpected("contents.content must be a base64 string")
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
        except ValueError as exc:
            raise GitHubAdapterError(
                GitHubAdapterErrorReason.MALFORMED_RESPONSE,
                "contents.content is not valid base64",
            ) from exc
        if _git_blob_sha(decoded) != blob_sha:
            _unexpected("Contents bytes do not match blob SHA")
        return decoded

    def create_blob(self, content: bytes) -> str:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        payload = self._request_json(
            "POST",
            f"/repos/{self._repository}/git/blobs",
            json_body={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
            expected_statuses={201},
        )
        sha = _sha(payload.get("sha"), "created blob.sha")
        if sha != _git_blob_sha(content):
            _unexpected("created blob SHA does not match submitted bytes")
        return sha

    def create_tree(self, base_tree_sha: str, updates: Mapping[str, str]) -> str:
        base_sha = _sha(base_tree_sha, "base_tree_sha")
        if not updates:
            raise ValueError("tree updates must not be empty")
        tree_entries = []
        for path, blob_sha in sorted(updates.items()):
            safe_path = _safe_v2_mutation_path(path)
            tree_entries.append(
                {
                    "path": safe_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": _sha(blob_sha, f"updates[{safe_path}]"),
                }
            )
        payload = self._request_json(
            "POST",
            f"/repos/{self._repository}/git/trees",
            json_body={"base_tree": base_sha, "tree": tree_entries},
            expected_statuses={201},
        )
        return _sha(payload.get("sha"), "created tree.sha")

    def create_commit(self, tree_sha: str, parent_sha: str, message: str) -> str:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("commit message must not be empty")
        payload = self._request_json(
            "POST",
            f"/repos/{self._repository}/git/commits",
            json_body={
                "message": message,
                "tree": _sha(tree_sha, "tree_sha"),
                "parents": [_sha(parent_sha, "parent_sha")],
            },
            expected_statuses={201},
        )
        return _sha(payload.get("sha"), "created commit.sha")

    def update_ref(
        self,
        branch: str,
        new_commit_sha: str,
        *,
        expected_old_sha: str,
        force: bool,
    ) -> bool:
        if force:
            raise GitHubAdapterError(
                GitHubAdapterErrorReason.NAMESPACE_VIOLATION,
                "force ref updates are forbidden",
            )
        expected = _sha(expected_old_sha, "expected_old_sha")
        new_sha = _sha(new_commit_sha, "new_commit_sha")
        if self.read_head(branch) != expected:
            return False
        response = self._request(
            "PATCH",
            f"/repos/{self._repository}/git/refs/heads/{_encoded_branch(branch)}",
            json_body={"sha": new_sha, "force": False},
            expected_statuses={200},
            allow_conflict=True,
        )
        if response.status_code in {409, 422}:
            if self.read_head(branch) != expected:
                return False
            raise GitHubAdapterError(
                GitHubAdapterErrorReason.INVALID_REQUEST,
                "non-force ref update was rejected without an observed head change",
                status_code=response.status_code,
            )
        payload = _json_object(response)
        obj = _dict(payload.get("object"), "updated ref.object")
        if _sha(obj.get("sha"), "updated ref.object.sha") != new_sha:
            _unexpected("updated ref does not point to submitted commit")
        return True

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        expected_statuses: set[int] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        response = self._request(
            method,
            path,
            params=params,
            json_body=json_body,
            expected_statuses=expected_statuses,
            allow_not_found=allow_not_found,
        )
        if allow_not_found and response.status_code == 404:
            return None
        return _json_object(response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        expected_statuses: set[int] | None = None,
        allow_not_found: bool = False,
        allow_conflict: bool = False,
    ) -> httpx.Response:
        expected = expected_statuses or {200}
        for attempt in range(1, self._transport_attempts + 1):
            try:
                response = self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": API_VERSION,
                    },
                )
            except httpx.RequestError as exc:
                if attempt < self._transport_attempts:
                    self._sleep(self._retry_delay)
                    continue
                raise GitHubAdapterError(
                    GitHubAdapterErrorReason.NETWORK_ERROR,
                    f"GitHub request failed after {attempt} attempts: {exc}",
                ) from exc
            if response.status_code in expected:
                return response
            if allow_not_found and response.status_code == 404:
                return response
            if allow_conflict and response.status_code in {409, 422}:
                return response
            if response.status_code in {401, 403}:
                raise GitHubAdapterError(
                    GitHubAdapterErrorReason.AUTH_FAILED,
                    "GitHub authentication or authorization failed",
                    status_code=response.status_code,
                )
            if response.status_code == 404:
                raise GitHubAdapterError(
                    GitHubAdapterErrorReason.NOT_FOUND,
                    f"GitHub object was not found: {path}",
                    status_code=404,
                )
            if response.status_code >= 500:
                if attempt < self._transport_attempts:
                    self._sleep(self._retry_delay)
                    continue
                raise GitHubAdapterError(
                    GitHubAdapterErrorReason.SERVER_ERROR,
                    f"GitHub returned HTTP {response.status_code} after {attempt} attempts",
                    status_code=response.status_code,
                )
            raise GitHubAdapterError(
                GitHubAdapterErrorReason.INVALID_REQUEST,
                f"GitHub returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        raise AssertionError("unreachable")


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubAdapterError(
            GitHubAdapterErrorReason.MALFORMED_RESPONSE,
            "GitHub response is not valid JSON",
            status_code=response.status_code,
        ) from exc
    if not isinstance(payload, dict):
        _unexpected("GitHub JSON root must be an object")
    return payload


def _dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _unexpected(f"{field} must be an object")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA_PATTERN.fullmatch(value):
        _unexpected(f"{field} must be a Git object SHA")
    return value


def _encoded_branch(branch: str) -> str:
    if branch != DATA_BRANCH:
        raise GitHubAdapterError(
            GitHubAdapterErrorReason.NAMESPACE_VIOLATION,
            "adapter is restricted to refs/heads/snapshot-data",
        )
    return quote(branch, safe="")


def _safe_repository_path(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        _unexpected("repository path is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GitHubAdapterError(
            GitHubAdapterErrorReason.NAMESPACE_VIOLATION,
            "repository path traversal is forbidden",
        )
    return value


def _safe_v2_mutation_path(path: str) -> str:
    safe = _safe_repository_path(path)
    if safe != V2_LATEST_PATH and not _DATED_PATH_PATTERN.fullmatch(safe):
        raise GitHubAdapterError(
            GitHubAdapterErrorReason.NAMESPACE_VIOLATION,
            f"mutation path is outside the V2 namespace: {safe}",
        )
    return safe


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _unexpected(detail: str) -> None:
    raise GitHubAdapterError(GitHubAdapterErrorReason.UNEXPECTED_RESPONSE, detail)
