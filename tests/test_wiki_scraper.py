import json

import pytest

from tools.scrape_wiki import extract_artifacts, next_flight_stream, script_urls


def flight_html(records):
    stream = 'a:["$","$L18",null,{"data":' + json.dumps(records, ensure_ascii=False, separators=(",", ":")) + '}]'
    payload = json.dumps([1, stream], ensure_ascii=False)
    return f"<html><script>self.__next_f.push({payload})</script></html>"


def test_next_flight_stream_decodes_embedded_payload():
    assert '"data"' in next_flight_stream(flight_html([]))


def test_extract_artifacts_fails_loudly_on_incomplete_catalog():
    record = {
        "id": 1, "value": "sample", "label_kor": "샘플", "tier": "common",
        "effect": {"sets": [], "content": "+1/2/3"}, "image": "/artifacts/sample.png", "level": 1,
    }
    with pytest.raises(RuntimeError, match="Unexpected Wiki artifact count"):
        extract_artifacts(flight_html([record]))


def test_script_urls_only_accepts_same_origin_next_chunks():
    html = """
        <script src="/_next/static/chunks/123.js?build=current"></script>
        <script src="https://evil.example/_next/static/chunks/no.js"></script>
        <script src="https://www.sephiria.wiki/not-a-chunk.js"></script>
    """
    assert script_urls(html) == ["https://www.sephiria.wiki/_next/static/chunks/123.js?build=current"]
