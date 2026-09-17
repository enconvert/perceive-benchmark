"""Smoke checks for the harness. Run: python -m pytest -q

Network-free. The vendored-file test needs the EnConvert gateway checkout
next to this repo (or ENCONVERT_GATEWAY_DIR) and is skipped otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

import analyze
from perceive_benchmark import ArmResult, load_urls

ROOT = Path(__file__).parent
GATEWAY = Path(os.getenv("ENCONVERT_GATEWAY_DIR", ROOT / ".." / "api" / "gateway")).resolve()

VENDORED = {
    "scorer.py": "services/v2_engine/quality/scorer.py",
    "instrumentation.py": "services/page_quality/instrumentation.py",
    "utils/url_registrable.py": "utils/url_registrable.py",
}


@pytest.mark.skipif(not GATEWAY.is_dir(), reason=f"gateway checkout not found at {GATEWAY}")
@pytest.mark.parametrize("local,upstream", sorted(VENDORED.items()))
def test_vendored_files_are_byte_identical_to_gateway(local: str, upstream: str) -> None:
    ours = hashlib.sha256((ROOT / local).read_bytes()).hexdigest()
    theirs = hashlib.sha256((GATEWAY / upstream).read_bytes()).hexdigest()
    assert ours == theirs, f"{local} drifted from gateway {upstream}; re-copy it verbatim"


def test_block_body_definition() -> None:
    assert analyze.is_block_body({"empty_body": 0.7})
    assert not analyze.is_block_body({"empty_body": 0.15})
    assert analyze.is_block_body({"anti_bot_challenge": 0.6})
    assert analyze.is_block_body({"http_error": 0.7})
    assert not analyze.is_block_body({"slow_render": 0.05, "domain_redirect": 0.1})


def test_corpus_is_200_with_50_legacy_and_four_categories() -> None:
    rows = load_urls(ROOT / "urls.txt")
    assert len(rows) == 200
    assert sum(1 for _, _, s in rows if s == "legacy") == 50
    assert {c for _, c, _ in rows} == {"bot-gated", "paywalled", "js-heavy", "control"}
    assert len({u for u, _, _ in rows}) == 200, "duplicate URL in urls.txt"


def _row(url: str, category: str, corpus: str, **arms: ArmResult) -> dict:
    return {"url": url, "category": category, "set": corpus,
            "arms": {name: asdict(res) for name, res in arms.items()}}


def test_analyze_synthetic_run(tmp_path: Path) -> None:
    # naive: 2xx Cloudflare page => silent block. enconvert: same page, labelled
    # is_blocked => not a miss. firecrawl: 2xx 403 body => masked error => miss.
    blocked = {"anti_bot_challenge": 0.6, "empty_body": 0.7}
    rows = [
        _row("https://a.example/", "bot-gated", "legacy",
             naive=ArmResult("naive", ok=True, arm_status=200, deductions=blocked,
                             quality=0.0, silent_block=True, elapsed_ms=400),
             enconvert=ArmResult("enconvert", ok=True, arm_status=200, reported_is_blocked=True,
                                 reported_quality=0.3, labelled=True, deductions=blocked,
                                 billed=False, cost=0.0, cost_unit="ops", elapsed_ms=9000),
             firecrawl=ArmResult("firecrawl", ok=True, arm_status=200, upstream_status=403,
                                 deductions={"http_error": 0.7}, silent_block=True,
                                 cost=1.0, cost_unit="credits", elapsed_ms=3000)),
        _row("https://b.example/", "control", "v2",
             naive=ArmResult("naive", ok=True, arm_status=200, quality=1.0, elapsed_ms=200),
             enconvert=ArmResult("enconvert", ok=True, arm_status=200, reported_quality=1.0,
                                 quality=1.0, billed=True, cost=1.0, cost_unit="ops", elapsed_ms=5000),
             firecrawl=ArmResult("firecrawl", ok=True, arm_status=200, quality=1.0,
                                 cost=1.0, cost_unit="credits", elapsed_ms=2000)),
        _row("https://c.example/", "paywalled", "v2",
             naive=ArmResult("naive", arm_status=403, error="http_403", deductions=blocked),
             enconvert=ArmResult("enconvert", ok=True, arm_status=200, quality=0.9,
                                 reported_quality=0.9, billed=True, cost=1.0, cost_unit="ops"),
             firecrawl=ArmResult("firecrawl", skipped="no FIRECRAWL_API_KEY")),
    ]
    for row in rows:
        row["arms"]["scrapingbee"] = asdict(ArmResult("scrapingbee", skipped="no SCRAPINGBEE_API_KEY"))
    src = tmp_path / "2026-09-20-test.json"
    src.write_text(json.dumps({"meta": {"schema": 2, "region": "test", "harness": "t",
                                        "generated_utc": "2026-09-20T00:00:00Z"}, "rows": rows}))
    out = tmp_path / "results.md"

    analyze.main([str(src), "--out", str(out)])

    summary = json.loads(src.read_text())["summary"]
    assert set(summary) == {"legacy-1", "v2-3"}
    v2 = summary["v2-3"]["arms"]
    assert v2["naive"] == {**v2["naive"], "attempted": 3, "silent_block": 1, "clean": 1, "loud_error": 1}
    assert v2["enconvert"]["silent_block"] == 0 and v2["enconvert"]["labelled"] == 1
    assert v2["enconvert"]["clean"] == 2 and v2["enconvert"]["billed"] == 2
    assert v2["enconvert"]["recovered_naive_misses"] == 0
    assert v2["firecrawl"]["silent_block"] == 1 and v2["firecrawl"]["attempted"] == 2
    assert v2["scrapingbee"] == {"skipped": "no SCRAPINGBEE_API_KEY"}
    assert summary["v2-3"]["by_category"]["bot-gated"]["arms"]["naive"]["silent_block"] == 1
    assert [m["arm"] for m in summary["v2-3"]["misses"]] == ["naive", "firecrawl"]

    md = out.read_text()
    assert "### Headline: legacy-1 (N=1)" in md and "### Headline: v2-3 (N=3)" in md
    assert "silently returned a block page as content: 33.3%** (1/3)" in md
    assert "| https://a.example/ | bot-gated | firecrawl | http_error 0.7 | 403 | 200 |" in md
    assert "skipped: no SCRAPINGBEE_API_KEY" in md

    # A folder input picks the newest file per region and ignores v1-schema files.
    (tmp_path / "2026-07-07-in-legacy-v1.json").write_text(json.dumps({"meta": {"harness": "v1"}}))
    analyze.main([str(tmp_path), "--out", str(out)])
    assert "## Run 2026-09-20 from `test`" in out.read_text()
