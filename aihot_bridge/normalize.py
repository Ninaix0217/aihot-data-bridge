from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


RSS_CATEGORY_MAP = {
    "AI 模型": "ai-models",
    "AI 产品": "ai-products",
    "行业动态": "industry",
    "论文": "paper",
    "技巧观点": "tip",
}
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


def normalize_api_item(raw: dict[str, Any], channel: str) -> dict[str, Any]:
    links = _dict(raw.get("links"))
    return {
        "id": _string_or_none(raw.get("id")),
        "item_type": "item",
        "category": _string_or_none(raw.get("category")),
        "title": _string_or_none(raw.get("title")),
        "description": _string_or_none(raw.get("summary")),
        "original_url": _string_or_none(links.get("original")),
        "aihot_url": _string_or_none(links.get("aihot")),
        "published_at": _iso_or_original(raw.get("publishedAt")),
        "collected_at": _iso_or_original(raw.get("discoveredAt")),
        "source": deepcopy(_dict(raw.get("source"))) or None,
        "source_channels": [channel],
        "metadata": _without_none(
            {
                "original_title": raw.get("originalTitle"),
                "score": raw.get("score"),
                "selected": raw.get("selected"),
                "reason": raw.get("reason"),
                "attribution": deepcopy(raw.get("attribution")),
            }
        ),
    }


def normalize_hot_topic(raw: dict[str, Any]) -> dict[str, Any]:
    links = _dict(raw.get("links"))
    return {
        "id": _string_or_none(raw.get("id")),
        "item_type": "hot_topic",
        "category": None,
        "title": _string_or_none(raw.get("title")),
        "description": None,
        "original_url": _string_or_none(links.get("original")),
        "aihot_url": _string_or_none(links.get("aihot")),
        "published_at": None,
        "collected_at": None,
        "source": deepcopy(_dict(raw.get("source"))) or None,
        "source_channels": ["hot_topics"],
        "metadata": _without_none(
            {
                "rank": raw.get("rank"),
                "latest_at": raw.get("latestAt"),
                "source_count": raw.get("sourceCount"),
                "signal_count": raw.get("signalCount"),
                "source_names": deepcopy(raw.get("sourceNames")),
                "story_url": links.get("story"),
            }
        ),
    }


def normalize_daily(raw: dict[str, Any], channel: str = "daily") -> dict[str, Any]:
    links = _dict(raw.get("links"))
    return {
        "id": None,
        "item_type": "daily_report",
        "category": None,
        "title": None,
        "description": _string_or_none(raw.get("lead")),
        "original_url": None,
        "aihot_url": _string_or_none(links.get("aihot")),
        "published_at": None,
        "collected_at": None,
        "source": {"name": "AIHOT"},
        "source_channels": [channel],
        "metadata": _without_none(
            {
                "date": raw.get("date"),
                "generated_at": raw.get("generatedAt"),
                "window_start": raw.get("windowStart"),
                "window_end": raw.get("windowEnd"),
                "sections": deepcopy(raw.get("sections")),
                "flashes": deepcopy(raw.get("flashes")),
                "attribution": deepcopy(raw.get("attribution")),
            }
        ),
    }


def parse_rss_items(
    xml_text: str,
    channel: str,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("malformed RSS XML") from exc

    output: list[dict[str, Any]] = []
    for element in root.findall("./channel/item"):
        published = _parse_rss_date(_text(element, "pubDate"))
        if published is None or not (window_start <= published <= window_end):
            continue
        description_html = _text(element, "description")
        parsed_html = _DescriptionParser(description_html)
        aihot_url = _text(element, "link")
        original_url = next(
            (url for url in parsed_html.links if url and "aihot.virxact.com" not in url),
            None,
        )
        raw_category = _text(element, "category")
        author = _text(element, "author")
        source_name = _author_name(author)
        guid = _text(element, "guid")
        is_daily = channel == "daily"
        output.append(
            {
                "id": guid,
                "item_type": "daily_report" if is_daily else "item",
                "category": None if is_daily else RSS_CATEGORY_MAP.get(raw_category, raw_category),
                "title": _text(element, "title"),
                "description": parsed_html.paragraphs[0] if parsed_html.paragraphs else None,
                "original_url": None if is_daily else original_url,
                "aihot_url": aihot_url,
                "published_at": published.isoformat().replace("+00:00", "Z"),
                "collected_at": None,
                "source": {"name": source_name} if source_name else None,
                "source_channels": [channel],
                "metadata": _without_none(
                    {
                        "rss_guid": guid,
                        "rss_author": author,
                        "rss_category": raw_category,
                    }
                ),
            }
        )
    return output


def deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    indexes: dict[tuple[str, str], int] = {}
    for item in items:
        keys = _identity_keys(item)
        match = next((indexes[key] for key in keys if key in indexes), None)
        if match is None:
            match = len(deduplicated)
            deduplicated.append(deepcopy(item))
        else:
            _merge_item(deduplicated[match], item)
        for key in keys:
            indexes[key] = match
    return deduplicated


def _identity_keys(item: dict[str, Any]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    stable_id = _normalized_text(item.get("id"))
    if stable_id:
        keys.append(("id", stable_id))
    original_url = _string_or_none(item.get("original_url"))
    if original_url:
        keys.append(("url", original_url.strip()))
        canonical = _canonical_url(original_url)
        if canonical:
            keys.append(("canonical_url", canonical))
    title = _normalized_text(item.get("title"))
    source = _dict(item.get("source"))
    source_name = _normalized_text(source.get("name"))
    published_at = _normalized_text(item.get("published_at"))
    if title and (source_name or published_at):
        keys.append(("title_source_time", f"{title}|{source_name}|{published_at}"))
    if not keys:
        aihot_url = _string_or_none(item.get("aihot_url"))
        if aihot_url:
            keys.append(("aihot_url", _canonical_url(aihot_url) or aihot_url))
    return keys


def _merge_item(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    channels = list(target.get("source_channels") or [])
    for channel in incoming.get("source_channels") or []:
        if channel not in channels:
            channels.append(channel)
    target["source_channels"] = channels

    for key in (
        "id",
        "category",
        "title",
        "description",
        "original_url",
        "aihot_url",
        "published_at",
        "collected_at",
        "source",
    ):
        if target.get(key) is None and incoming.get(key) is not None:
            target[key] = deepcopy(incoming[key])

    incoming_metadata = incoming.get("metadata") or {}
    target_metadata = target.setdefault("metadata", {})
    for key, value in incoming_metadata.items():
        if key not in target_metadata or target_metadata[key] is None:
            target_metadata[key] = deepcopy(value)


def _canonical_url(value: str) -> str | None:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query, doseq=True), "")
    )


class _DescriptionParser(HTMLParser):
    def __init__(self, html: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.links: list[str] = []
        self._paragraph_parts: list[str] | None = None
        self.feed(html or "")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            self._paragraph_parts = []
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_data(self, data: str) -> None:
        if self._paragraph_parts is not None:
            self._paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._paragraph_parts is not None:
            text = re.sub(r"\s+", " ", "".join(self._paragraph_parts)).strip()
            if text:
                self.paragraphs.append(text)
            self._paragraph_parts = None


def _text(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _author_name(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\((.*)\)\s*$", value)
    return match.group(1).strip() if match else value.strip()


def _parse_rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_or_original(value: Any) -> str | None:
    # API timestamps are already ISO 8601. Preserve their exact representation
    # so the bridge does not silently add or remove precision.
    return _string_or_none(value)


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _string_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
