from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .candidate_v2 import (
    CandidateV2Error,
    evaluate_candidate_completeness,
    validate_candidate_v2,
    validated_candidate_bytes,
)
from .config import Settings
from .retrieval_v2 import CandidateBuildResult, V2CandidateService
from .upstream import UpstreamClient


async def build_live_candidate(report_day: date) -> CandidateBuildResult:
    settings = Settings.from_env()
    timeout = httpx.Timeout(
        settings.request_timeout_seconds,
        connect=settings.connect_timeout_seconds,
    )
    async with httpx.AsyncClient(
        base_url=settings.api_base_url,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "aihot-data-bridge/0.1 v2-local"},
    ) as client:
        service = V2CandidateService(
            UpstreamClient(client, max_retries=settings.max_retries),
            settings,
        )
        return await service.candidate_for(report_day)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or validate a local target-anchored V2 candidate"
    )
    parser.add_argument("--report-date")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.check is not None:
            if args.report_date is not None or args.output is not None:
                raise ValueError("--check cannot be combined with --report-date or --output")
            payload = _load_json(args.check)
            metadata = validate_candidate_v2(payload)
            print(
                json.dumps(
                    {
                        "state": "VALID_COMPLETE",
                        "target_report_date": metadata.target_report_date.isoformat(),
                        "path": str(args.check),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.report_date is None or args.output is None:
            raise ValueError("--report-date and --output are required for generation")
        report_day = _parse_report_date(args.report_date)
        result = asyncio.run(build_live_candidate(report_day))
        content = validated_candidate_bytes(result.payload)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(content)
        _print_build_summary(result, args.output, content)
        return 0
    except (CandidateV2Error, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"V2 candidate failed: {exc}", file=sys.stderr)
        return 1


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate JSON root must be an object")
    return payload


def _parse_report_date(value: str) -> date:
    if len(value) != 10:
        raise ValueError("--report-date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--report-date must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError("--report-date must use YYYY-MM-DD")
    return parsed


def _print_build_summary(
    result: CandidateBuildResult,
    output: Path,
    content: bytes,
) -> None:
    payload = result.payload
    evaluation = evaluate_candidate_completeness(payload)
    channels = {}
    for channel in ("selected", "all", "paper", "hot_topics", "daily"):
        coverage = payload["coverage"][channel]
        source_range = coverage["source_range"]
        channels[channel] = {
            "status": coverage["status"],
            "source": coverage["source"],
            "items": coverage["items"],
            "pages": source_range["pages_fetched"],
            "oldest_published_at": source_range["oldest_published_at"],
            "proof_basis": source_range["proof_basis"],
            "source_range_state": source_range["state"],
        }
    print(
        json.dumps(
            {
                "state": evaluation.state.value,
                "target_report_date": payload["target_report_date"],
                "report_window": payload["report_window"],
                "retrieval_as_of": payload["retrieval"]["as_of"],
                "channels": channels,
                "summary": payload["summary"],
                "logical_requests": result.logical_requests,
                "duration_seconds": round(result.duration_seconds, 3),
                "sha256": hashlib.sha256(content).hexdigest(),
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
