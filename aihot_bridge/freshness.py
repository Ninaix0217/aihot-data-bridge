from __future__ import annotations

import argparse
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


def fetch_snapshot(
    url: str,
    *,
    timeout_seconds: float = 20,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
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
            payload = json.load(response)
    except FreshnessCheckError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FreshnessCheckError(f"public snapshot request failed: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FreshnessCheckError(f"public snapshot is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise FreshnessCheckError("snapshot JSON root must be an object")
    return payload


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the freshness of the public AIHOT snapshot"
    )
    parser.add_argument("url", nargs="?", default=DEFAULT_SNAPSHOT_URL)
    parser.add_argument("--max-age-minutes", type=float, default=90)
    parser.add_argument("--expected-generated-at")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = check_public_snapshot(
            args.url,
            max_age=timedelta(minutes=args.max_age_minutes),
            expected_generated_at=args.expected_generated_at,
            attempts=args.attempts,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except (FreshnessCheckError, ValueError) as exc:
        _write_github_summary(url=args.url, error=exc)
        print(f"freshness check failed: {exc}", file=sys.stderr)
        return 1

    _write_github_summary(url=args.url, result=result)
    print(json.dumps(_result_as_dict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
