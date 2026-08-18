from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_SNAPSHOT_URL = (
    "https://ninaix0217.github.io/aihot-data-bridge/today.json"
)
CHANNELS = ("selected", "all", "paper", "hot_topics", "daily")


class FreshnessCheckError(RuntimeError):
    """The public snapshot cannot currently be trusted as fresh."""


@dataclass(frozen=True)
class FreshnessResult:
    payload: dict[str, Any]
    generated_at: datetime
    age_seconds: float


@dataclass(frozen=True)
class SnapshotDocument:
    payload: dict[str, Any]
    content: bytes
    http_status: int = 200


@dataclass(frozen=True)
class PublicSnapshotObservation:
    url: str
    result: str
    http_status: int | None
    generated_at: str | None
    sha256: str | None
    freshness: FreshnessResult | None
    error: str | None


@dataclass(frozen=True)
class MatchingSnapshotResult:
    canonical: FreshnessResult | None
    dated: FreshnessResult | None
    canonical_observation: PublicSnapshotObservation
    dated_observation: PublicSnapshotObservation
    expected_generated_at: str
    expected_sha256: str
    sha256: str | None
    attempts_used: int
    elapsed_seconds: float
    state: str


class PublicVerificationError(FreshnessCheckError):
    """The dated production path did not expose the deployment artifact."""

    def __init__(self, message: str, result: MatchingSnapshotResult):
        super().__init__(message)
        self.result = result


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FreshnessCheckError(f"{field} is missing or is not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FreshnessCheckError(f"{field} is not valid ISO 8601") from exc
    if parsed.tzinfo is None:
        raise FreshnessCheckError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def evaluate_snapshot(
    payload: Any,
    *,
    now: datetime,
    max_age: timedelta,
    expected_generated_at: str | None = None,
) -> FreshnessResult:
    if not isinstance(payload, dict):
        raise FreshnessCheckError("snapshot JSON root must be an object")
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")

    generated_at = _parse_timestamp(payload.get("generated_at"), "generated_at")
    utc_now = now.astimezone(timezone.utc)
    age_seconds = (utc_now - generated_at).total_seconds()
    if age_seconds < -300:
        raise FreshnessCheckError("generated_at is more than five minutes in the future")
    if age_seconds > max_age.total_seconds():
        raise FreshnessCheckError(
            f"snapshot is stale: age={age_seconds:.1f}s "
            f"limit={max_age.total_seconds():.1f}s"
        )

    if expected_generated_at is not None:
        expected = _parse_timestamp(
            expected_generated_at, "expected_generated_at"
        )
        if generated_at < expected:
            raise FreshnessCheckError(
                "public snapshot has not reached the deployment candidate: "
                f"public={generated_at.isoformat()} expected={expected.isoformat()}"
            )

    return FreshnessResult(
        payload=payload,
        generated_at=generated_at,
        age_seconds=age_seconds,
    )


def fetch_snapshot_document(
    url: str,
    *,
    timeout_seconds: float = 20,
    opener: Callable[..., Any] | None = None,
) -> SnapshotDocument:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "aihot-snapshot-freshness/1",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if status != 200:
                raise FreshnessCheckError(
                    f"public snapshot returned HTTP {status}"
                )
            content = response.read()
    except FreshnessCheckError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FreshnessCheckError(f"public snapshot request failed: {exc}") from exc
    try:
        payload = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FreshnessCheckError(f"public snapshot is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise FreshnessCheckError("snapshot JSON root must be an object")
    return SnapshotDocument(payload=payload, content=content)


def fetch_snapshot(
    url: str,
    *,
    timeout_seconds: float = 20,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    return fetch_snapshot_document(
        url,
        timeout_seconds=timeout_seconds,
        opener=opener,
    ).payload


def check_public_snapshot(
    url: str,
    *,
    max_age: timedelta,
    expected_generated_at: str | None = None,
    attempts: int = 1,
    interval_seconds: float = 5,
    timeout_seconds: float = 20,
    now_factory: Callable[[], datetime] | None = None,
) -> FreshnessResult:
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    current_time = now_factory or (lambda: datetime.now(timezone.utc))
    last_error: FreshnessCheckError | None = None

    for attempt in range(1, attempts + 1):
        try:
            payload = fetch_snapshot(url, timeout_seconds=timeout_seconds)
            return evaluate_snapshot(
                payload,
                now=current_time(),
                max_age=max_age,
                expected_generated_at=expected_generated_at,
            )
        except FreshnessCheckError as exc:
            last_error = exc
            print(
                f"freshness attempt {attempt}/{attempts} failed: {exc}",
                file=sys.stderr,
            )
            if attempt < attempts:
                time.sleep(interval_seconds)

    assert last_error is not None
    raise last_error


def check_matching_public_snapshots(
    canonical_url: str,
    dated_url: str,
    *,
    max_age: timedelta,
    expected_generated_at: str,
    expected_sha256: str,
    attempts: int = 1,
    interval_seconds: float = 5,
    timeout_seconds: float = 20,
    now_factory: Callable[[], datetime] | None = None,
) -> MatchingSnapshotResult:
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    if interval_seconds < 0:
        raise ValueError("interval_seconds cannot be negative")
    normalized_expected_sha256 = expected_sha256.strip().lower()
    if (
        len(normalized_expected_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in normalized_expected_sha256
        )
    ):
        raise ValueError("expected_sha256 must be a 64-character hex digest")

    current_time = now_factory or (lambda: datetime.now(timezone.utc))
    started_at = time.monotonic()
    canonical_observation: PublicSnapshotObservation | None = None
    dated_observation: PublicSnapshotObservation | None = None

    for attempt in range(1, attempts + 1):
        attempt_started_at = time.monotonic()

        # Observe both paths independently. A canonical failure must never
        # prevent the dated production consumer path from being evaluated.
        canonical_observation = _observe_deployment_snapshot(
            canonical_url,
            role="canonical",
            now=current_time(),
            max_age=max_age,
            expected_generated_at=expected_generated_at,
            expected_sha256=normalized_expected_sha256,
            timeout_seconds=timeout_seconds,
        )
        dated_observation = _observe_deployment_snapshot(
            dated_url,
            role="dated",
            now=current_time(),
            max_age=max_age,
            expected_generated_at=expected_generated_at,
            expected_sha256=normalized_expected_sha256,
            timeout_seconds=timeout_seconds,
        )
        _log_deployment_attempt(
            attempt=attempt,
            attempts=attempts,
            expected_generated_at=expected_generated_at,
            canonical=canonical_observation,
            dated=dated_observation,
        )

        if dated_observation.result == "DATED_VERIFIED":
            state = _success_state_for_canonical(canonical_observation)
            return _matching_result(
                canonical=canonical_observation,
                dated=dated_observation,
                expected_generated_at=expected_generated_at,
                expected_sha256=normalized_expected_sha256,
                attempts_used=attempt,
                elapsed_seconds=time.monotonic() - started_at,
                state=state,
            )

        if attempt < attempts:
            # Keep attempt starts approximately interval_seconds apart. This
            # makes the workflow timeout calculable even when requests stall.
            attempt_elapsed = time.monotonic() - attempt_started_at
            time.sleep(max(0.0, interval_seconds - attempt_elapsed))

    assert canonical_observation is not None
    assert dated_observation is not None
    failure_state = _failure_state_for_dated(dated_observation)
    result = _matching_result(
        canonical=canonical_observation,
        dated=dated_observation,
        expected_generated_at=expected_generated_at,
        expected_sha256=normalized_expected_sha256,
        attempts_used=attempts,
        elapsed_seconds=time.monotonic() - started_at,
        state=failure_state,
    )
    raise PublicVerificationError(
        f"{failure_state}: {dated_observation.error or dated_observation.result}",
        result,
    )


def _observe_deployment_snapshot(
    url: str,
    *,
    role: str,
    now: datetime,
    max_age: timedelta,
    expected_generated_at: str,
    expected_sha256: str,
    timeout_seconds: float,
) -> PublicSnapshotObservation:
    try:
        document = fetch_snapshot_document(url, timeout_seconds=timeout_seconds)
    except (FreshnessCheckError, ValueError) as exc:
        return PublicSnapshotObservation(
            url=url,
            result="PUBLIC_READ_ERROR",
            http_status=None,
            generated_at=None,
            sha256=None,
            freshness=None,
            error=str(exc),
        )

    content_sha256 = hashlib.sha256(document.content).hexdigest()
    generated_at = document.payload.get("generated_at")
    observed_generated_at = generated_at if isinstance(generated_at, str) else None

    from .snapshot import (
        SnapshotRejected,
        ensure_trustworthy_snapshot,
        validate_snapshot,
    )

    try:
        validate_snapshot(document.payload)
        ensure_trustworthy_snapshot(document.payload)
    except (SnapshotRejected, ValueError) as exc:
        return PublicSnapshotObservation(
            url=url,
            result="PUBLIC_READ_ERROR",
            http_status=document.http_status,
            generated_at=observed_generated_at,
            sha256=content_sha256,
            freshness=None,
            error=f"snapshot schema/trust validation failed: {exc}",
        )

    try:
        freshness = evaluate_snapshot(
            document.payload,
            now=now,
            max_age=max_age,
            expected_generated_at=expected_generated_at,
        )
    except FreshnessCheckError as exc:
        message = str(exc)
        result = (
            "PROPAGATION_LAG"
            if "has not reached the deployment candidate" in message
            else "PUBLIC_READ_ERROR"
        )
        return PublicSnapshotObservation(
            url=url,
            result=result,
            http_status=document.http_status,
            generated_at=observed_generated_at,
            sha256=content_sha256,
            freshness=None,
            error=message,
        )

    if content_sha256 != expected_sha256:
        result = "ARTIFACT_MISMATCH" if role == "dated" else "PARITY_MISMATCH"
        return PublicSnapshotObservation(
            url=url,
            result=result,
            http_status=document.http_status,
            generated_at=observed_generated_at,
            sha256=content_sha256,
            freshness=freshness,
            error=(
                "public content SHA-256 does not match the deployment artifact: "
                f"public={content_sha256} expected={expected_sha256}"
            ),
        )

    return PublicSnapshotObservation(
        url=url,
        result="DATED_VERIFIED" if role == "dated" else "VERIFIED",
        http_status=document.http_status,
        generated_at=observed_generated_at,
        sha256=content_sha256,
        freshness=freshness,
        error=None,
    )


def _log_deployment_attempt(
    *,
    attempt: int,
    attempts: int,
    expected_generated_at: str,
    canonical: PublicSnapshotObservation,
    dated: PublicSnapshotObservation,
) -> None:
    print(
        f"public verification attempt {attempt}/{attempts}: "
        f"expected_generated_at={expected_generated_at}",
        file=sys.stderr,
    )
    for label, observation in (("canonical", canonical), ("dated", dated)):
        details = (
            f"{label}: result={observation.result} "
            f"http={observation.http_status or 'unavailable'} "
            f"generated_at={observation.generated_at or 'unavailable'}"
        )
        if observation.error:
            details += f" error={observation.error}"
        print(details, file=sys.stderr)


def _success_state_for_canonical(observation: PublicSnapshotObservation) -> str:
    if observation.result == "VERIFIED":
        return "PUBLIC_PARITY_CONFIRMED"
    if observation.result == "PROPAGATION_LAG":
        return "CANONICAL_PROPAGATION_LAG"
    if observation.result == "PUBLIC_READ_ERROR":
        return "CANONICAL_PUBLIC_READ_ERROR"
    return "CANONICAL_PARITY_MISMATCH"


def _failure_state_for_dated(observation: PublicSnapshotObservation) -> str:
    if observation.result == "PROPAGATION_LAG":
        return "DATED_PROPAGATION_TIMEOUT"
    if observation.result == "ARTIFACT_MISMATCH":
        return "DATED_ARTIFACT_MISMATCH"
    return "DATED_PUBLIC_READ_ERROR"


def _matching_result(
    *,
    canonical: PublicSnapshotObservation,
    dated: PublicSnapshotObservation,
    expected_generated_at: str,
    expected_sha256: str,
    attempts_used: int,
    elapsed_seconds: float,
    state: str,
) -> MatchingSnapshotResult:
    return MatchingSnapshotResult(
        canonical=canonical.freshness,
        dated=dated.freshness,
        canonical_observation=canonical,
        dated_observation=dated,
        expected_generated_at=expected_generated_at,
        expected_sha256=expected_sha256,
        sha256=dated.sha256,
        attempts_used=attempts_used,
        elapsed_seconds=elapsed_seconds,
        state=state,
    )


def _result_as_dict(result: FreshnessResult) -> dict[str, Any]:
    payload = result.payload
    return {
        "status": "fresh",
        "generated_at": payload["generated_at"],
        "age_seconds": round(result.age_seconds, 1),
        "raw_items": payload.get("summary", {}).get("raw_items"),
        "deduplicated_items": payload.get("summary", {}).get(
            "deduplicated_items"
        ),
        "coverage": {
            channel: {
                field: payload.get("coverage", {})
                .get(channel, {})
                .get(field)
                for field in ("status", "source", "items")
            }
            for channel in CHANNELS
        },
    }


def _write_github_summary(
    *,
    url: str,
    result: FreshnessResult | None = None,
    error: Exception | None = None,
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = ["## Public snapshot freshness", "", f"- URL: `{url}`"]
    if result is not None:
        report = _result_as_dict(result)
        lines.extend(
            [
                "- Result: `FRESH`",
                f"- generated_at: `{report['generated_at']}`",
                f"- age_seconds: `{report['age_seconds']}`",
                f"- raw_items: `{report['raw_items']}`",
                f"- deduplicated_items: `{report['deduplicated_items']}`",
                "",
                "| channel | status | source | items |",
                "| --- | --- | --- | ---: |",
            ]
        )
        for channel in CHANNELS:
            entry = report["coverage"][channel]
            lines.append(
                f"| {channel} | {entry['status']} | "
                f"{entry['source']} | {entry['items']} |"
            )
    else:
        lines.extend(["- Result: `FAILED`", f"- Error: `{error}`"])

    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _observation_as_dict(
    observation: PublicSnapshotObservation,
) -> dict[str, Any]:
    return {
        "url": observation.url,
        "result": observation.result,
        "http_status": observation.http_status,
        "generated_at": observation.generated_at,
        "sha256": observation.sha256,
        "error": observation.error,
    }


def _matching_result_as_dict(result: MatchingSnapshotResult) -> dict[str, Any]:
    return {
        "status": (
            "success"
            if result.dated_observation.result == "DATED_VERIFIED"
            else "failure"
        ),
        "final_verification_state": result.state,
        "expected_generated_at": result.expected_generated_at,
        "expected_sha256": result.expected_sha256,
        "attempts_used": result.attempts_used,
        "elapsed_seconds": round(result.elapsed_seconds, 1),
        "dated": _observation_as_dict(result.dated_observation),
        "canonical": _observation_as_dict(result.canonical_observation),
    }


def _write_matching_github_summary(result: MatchingSnapshotResult) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    success = result.dated_observation.result == "DATED_VERIFIED"
    lines = [
        "## Public deployment verification",
        "",
        f"- Expected generated_at: `{result.expected_generated_at}`",
        f"- Expected artifact SHA-256: `{result.expected_sha256}`",
        f"- Attempts used: `{result.attempts_used}`",
        f"- Elapsed wait: `{result.elapsed_seconds:.1f}s`",
        "",
        "### Dated consumer (HARD SLO)",
        "",
        f"- URL: `{result.dated_observation.url}`",
        f"- Result: `{result.dated_observation.result}`",
        f"- HTTP: `{result.dated_observation.http_status or 'unavailable'}`",
        f"- generated_at: `{result.dated_observation.generated_at or 'unavailable'}`",
        f"- SHA-256: `{result.dated_observation.sha256 or 'unavailable'}`",
        "",
        "### Canonical (SOFT PARITY)",
        "",
        f"- URL: `{result.canonical_observation.url}`",
        f"- Result: `{result.canonical_observation.result}`",
        f"- HTTP: `{result.canonical_observation.http_status or 'unavailable'}`",
        "- generated_at: "
        f"`{result.canonical_observation.generated_at or 'unavailable'}`",
        f"- SHA-256: `{result.canonical_observation.sha256 or 'unavailable'}`",
        "",
        f"### Final verification state: `{result.state}`",
        "",
        f"- Workflow verification result: `{'SUCCESS' if success else 'FAILURE'}`",
    ]
    if result.dated_observation.error:
        lines.append(f"- Dated detail: `{result.dated_observation.error}`")
    if result.canonical_observation.error:
        lines.append(f"- Canonical detail: `{result.canonical_observation.error}`")

    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the freshness of the public AIHOT snapshot"
    )
    parser.add_argument("url", nargs="?", default=DEFAULT_SNAPSHOT_URL)
    parser.add_argument("--max-age-minutes", type=float, default=90)
    parser.add_argument("--expected-generated-at")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--matching-url")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.matching_url:
            if not args.expected_generated_at:
                raise ValueError(
                    "--matching-url requires --expected-generated-at"
                )
            if not args.expected_sha256:
                raise ValueError("--matching-url requires --expected-sha256")
            matching = check_matching_public_snapshots(
                args.url,
                args.matching_url,
                max_age=timedelta(minutes=args.max_age_minutes),
                expected_generated_at=args.expected_generated_at,
                expected_sha256=args.expected_sha256,
                attempts=args.attempts,
                interval_seconds=args.interval_seconds,
                timeout_seconds=args.timeout_seconds,
            )
            _write_matching_github_summary(matching)
            print(json.dumps(_matching_result_as_dict(matching), ensure_ascii=False))
            return 0

        result = check_public_snapshot(
            args.url,
            max_age=timedelta(minutes=args.max_age_minutes),
            expected_generated_at=args.expected_generated_at,
            attempts=args.attempts,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except PublicVerificationError as exc:
        _write_matching_github_summary(exc.result)
        print(f"public deployment verification failed: {exc}", file=sys.stderr)
        print(json.dumps(_matching_result_as_dict(exc.result), ensure_ascii=False))
        return 1
    except (FreshnessCheckError, ValueError) as exc:
        _write_github_summary(url=args.url, error=exc)
        print(f"freshness check failed: {exc}", file=sys.stderr)
        return 1

    _write_github_summary(url=args.url, result=result)
    print(json.dumps(_result_as_dict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
