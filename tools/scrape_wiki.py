from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
BASE_URL = "https://www.sephiria.wiki"
ARTIFACT_URL = f"{BASE_URL}/artifact"
TABLET_URL = f"{BASE_URL}/large"
USER_AGENT = "SephiriaBackpackSolver/1.0 (catalog updater)"


class ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        source = dict(attrs).get("src")
        if source:
            self.sources.append(source)


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} while fetching {url}")
        return response.read().decode("utf-8")


def script_urls(page_html: str) -> list[str]:
    parser = ScriptParser()
    parser.feed(page_html)
    result = []
    for source in parser.sources:
        url = urljoin(BASE_URL, source)
        parsed = urlparse(url)
        if parsed.netloc == urlparse(BASE_URL).netloc and parsed.path.startswith("/_next/static/chunks/"):
            result.append(url)
    return result


def next_flight_stream(page_html: str) -> str:
    decoder = json.JSONDecoder()
    marker = "self.__next_f.push("
    cursor = 0
    chunks: list[str] = []
    while True:
        cursor = page_html.find(marker, cursor)
        if cursor < 0:
            break
        start = cursor + len(marker)
        try:
            payload, consumed = decoder.raw_decode(page_html[start:])
        except json.JSONDecodeError:
            cursor = start
            continue
        if isinstance(payload, list) and len(payload) >= 2 and payload[0] == 1 and isinstance(payload[1], str):
            chunks.append(payload[1])
        cursor = start + consumed
    if not chunks:
        raise RuntimeError("No Next.js flight data found on artifact page")
    return "".join(chunks)


def extract_artifacts(page_html: str) -> list[dict]:
    stream = next_flight_stream(page_html)
    marker = '"data":['
    start = stream.find(marker)
    if start < 0:
        raise RuntimeError("Artifact data array was not found in Next.js flight data")
    artifacts, _ = json.JSONDecoder().raw_decode(stream[start + len('"data":'):])
    if not isinstance(artifacts, list) or len(artifacts) < 200:
        raise RuntimeError(f"Unexpected Wiki artifact count: {len(artifacts) if isinstance(artifacts, list) else 'invalid'}")
    required = {"id", "value", "label_kor", "tier", "effect", "image", "level"}
    cleaned = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not required.issubset(artifact):
            raise RuntimeError(f"Unexpected artifact schema near: {artifact!r}")
        if artifact.get("disabled") not in (None, "$undefined", False):
            continue
        cleaned.append({key: value for key, value in artifact.items() if value != "$undefined"})
    return cleaned


def extract_tablets(page_html: str) -> dict:
    bundle = None
    bundle_url = None
    for url in script_urls(page_html):
        source = fetch_text(url)
        if 'value:"chivalry"' in source and "ko_label" in source and "approximation:" in source:
            bundle = source
            bundle_url = url
            break
    if bundle is None:
        raise RuntimeError("Unable to locate the Wiki tablet data/rule bundle")
    helper = Path(__file__).with_name("wiki_extract_tablets.js")
    completed = subprocess.run(
        ["node", str(helper)], input=bundle, text=True, encoding="utf-8",
        capture_output=True, check=False, timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Tablet extractor failed: {completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    payload["bundleUrl"] = bundle_url
    return payload


def write_catalogs(artifacts: list[dict], tablets: dict) -> None:
    generated_at = datetime.now(UTC).isoformat()
    artifact_payload = {
        "source": ARTIFACT_URL, "generatedAt": generated_at,
        "count": len(artifacts), "artifacts": artifacts,
    }
    tablet_payload = {
        "source": TABLET_URL, "bundleUrl": tablets["bundleUrl"],
        "generatedAt": generated_at, "count": len(tablets["tablets"]),
        "tablets": tablets["tablets"], "candidates": tablets["candidates"],
    }
    ASSETS.mkdir(exist_ok=True)
    (ASSETS / "wiki_artifacts.json").write_text(
        json.dumps(artifact_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    with gzip.open(ASSETS / "wiki_tablets.json.gz", "wt", encoding="utf-8", newline="") as handle:
        json.dump(tablet_payload, handle, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh solver catalogs from sephiria.wiki")
    parser.parse_args()
    artifact_html = fetch_text(ARTIFACT_URL)
    tablet_html = fetch_text(TABLET_URL)
    artifacts = extract_artifacts(artifact_html)
    tablets = extract_tablets(tablet_html)
    write_catalogs(artifacts, tablets)
    print(f"Wrote {len(artifacts)} artifacts and {len(tablets['tablets'])} tablets from the Wiki.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Wiki catalog update failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
