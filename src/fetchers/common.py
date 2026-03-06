import json
import time
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from src.logger import get_logger

logger = get_logger(__name__)

# Realistic browser headers — required for sources that block bots
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_DEFAULT_TIMEOUT = 25


def get_html(url: str, extra_headers: dict = None, timeout: int = _DEFAULT_TIMEOUT) -> str:
    headers = {**_HEADERS, **(extra_headers or {})}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_rss(url: str) -> list[dict]:
    """
    Fetch and parse an RSS 2.0 feed. Returns a list of item dicts.
    Each dict contains flat string values keyed by tag name (namespace stripped).
    """
    html = get_html(
        url,
        extra_headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"},
    )
    try:
        root = ET.fromstring(html)
    except ET.ParseError as e:
        logger.warning(f"[common] RSS parse error for {url}: {e}")
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for item in channel.findall("item"):
        entry: dict[str, str] = {}
        for child in item:
            tag = child.tag
            if "}" in tag:
                tag = tag.split("}", 1)[1]
            entry[tag] = (child.text or "").strip()
        items.append(entry)

    return items


def extract_next_data(html: str) -> dict:
    """
    Extract the __NEXT_DATA__ JSON blob embedded in a Next.js page.
    Returns empty dict if not found or unparseable.
    """
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if not script or not script.string:
        return {}
    try:
        return json.loads(script.string)
    except json.JSONDecodeError:
        return {}


def find_in_next_data(data: dict, key: str) -> list:
    """
    Recursively search a __NEXT_DATA__ dict for any list value associated
    with the given key. Returns the first matching list found, or [].
    """
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key and isinstance(v, list):
                return v
            result = find_in_next_data(v, key)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = find_in_next_data(item, key)
            if result:
                return result
    return []


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def polite_sleep(seconds: float = 1.5) -> None:
    time.sleep(seconds)


def parse_rfc2822_date(date_str: str) -> str:
    """Convert RFC 2822 pubDate string to YYYY-MM-DD. Returns original string on failure."""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
    except Exception:
        return date_str
