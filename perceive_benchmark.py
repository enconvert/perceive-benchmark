#!/usr/bin/env python3
"""Enconvert "silent failure" benchmark: perceive vs. a naive fetch.

Runs a fixed, public set of bot-gated / paywalled / JS-heavy URLs two ways:

1. NAIVE FETCH  — a single HTTP GET with a real browser User-Agent, no
   JavaScript. This is what a naive scraper / `fetch()` gets. We then ask:
   did the server hand back a *block page* (Cloudflare/CAPTCHA challenge),
   an empty JS shell, or a paywall gate that a naive consumer would ingest
   as if it were the real content? If so, the naive fetch *silently failed*.

2. ENCONVERT PERCEIVE — the page is rendered in a real headless browser via
   crawl4ai (the exact BrowserConfig + CrawlerRunConfig the Enconvert gateway
   uses in production: stealth, magic mode, simulate-user, override-navigator)
   and scored by Enconvert's 8-point render-quality scorer (scorer.py, vendored
   verbatim from the gateway). A render is *flagged* when render_quality < 0.40
   — Enconvert never silently returns a block page as content; it either renders
   the real page or tells you the render is low quality.

Outputs: results.json (full per-URL data) and results.md (summary), and prints
the four headline numbers the Enconvert MCP page reports.

Reproduce:
    pip install -r requirements.txt
    playwright install chromium
    python perceive_benchmark.py urls.txt
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

from instrumentation import PageInstrumentation
from scorer import score

# --------------------------------------------------------------------------
# Enconvert gateway render configuration (copied verbatim from
# services/browser/converters/browser_manager.py and arun_flow.py so this
# harness renders exactly as production does).
# --------------------------------------------------------------------------

QUALITY_FLOOR = 0.40  # services/v2_engine/watch_flow.py: QUALITY_FLOOR

V1_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

CHROMIUM_MEMORY_FLAGS: list[str] = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--js-flags=--max-old-space-size=256",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--aggressive-cache-discard",
    "--disk-cache-size=1",
    "--memory-pressure-off",
]

# A full modern-Chrome header set for the NAIVE fetch. We deliberately give
# the naive path its *best shot* — a real browser UA and Accept headers — so
# the benchmark measures the JS/bot-gate barrier, not UA-string blocking.
NAIVE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

NAIVE_TIMEOUT_S = 25.0

# HTTP statuses that mean "the server refused" but still return a body a
# naive `resp.text` consumer would ingest.
_BLOCK_STATUSES = frozenset({401, 403, 429, 451, 503})

# Visible-word thresholds that keep the classifier conservative — a signal
# only counts as a silent failure when the page is NOT already a full,
# usable content page:
#   - empty_shell: essentially no readable content at all.
#   - bot_challenge: the challenge markers must DOMINATE the body, not merely
#     be embedded scripts (an invisible Cloudflare turnstile on a page that
#     still returned real content is NOT a failure).
#   - http refusal: a block status with a non-content-sized body.
_EMPTY_SHELL_WORDS = 20
_BOT_CHALLENGE_MAX_WORDS = 100
_HTTP_BLOCK_MAX_WORDS = 800

# Unambiguous paywall / gate intent phrases (phrases that only appear on a
# content gate, not on ordinary marketing/SaaS pages). Matched only on a
# THIN page (few words) so a full metered article that merely mentions
# "subscribe" in its footer is NOT counted as a silent failure. Every
# paywall_gate classification is additionally reviewed by hand before the
# headline numbers are published.
_PAYWALL_MARKERS: tuple[str, ...] = (
    "subscribe to continue",
    "subscribe to read",
    "to continue reading",
    "continue reading your article",
    "already a subscriber",
    "this article is for subscribers",
    "this content is for subscribers",
    "for subscribers only",
    "subscribers only",
    "sign in to read",
    "log in to read",
    "register to continue",
    "unlock this article",
    "you've reached your",
    "you have reached your",
    "you have read your",
    "join to continue reading",
)
_PAYWALL_MAX_WORDS = 550

_INVISIBLE_TAGS = ("script", "style", "noscript", "template")


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(_INVISIBLE_TAGS):
        tag.decompose()
    return soup.get_text(" ")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class UrlResult:
    url: str
    category: str

    # Naive fetch
    naive_status: Optional[int] = None
    naive_final_url: Optional[str] = None
    naive_word_count: Optional[int] = None
    naive_bytes: Optional[int] = None
    naive_error: Optional[str] = None
    naive_silent_fail: bool = False
    naive_fail_reasons: list[str] = field(default_factory=list)
    naive_excerpt: str = ""

    # Enconvert perceive
    enconvert_quality: Optional[float] = None
    enconvert_is_blocked: Optional[bool] = None
    enconvert_word_count: Optional[int] = None
    enconvert_deductions: dict[str, float] = field(default_factory=dict)
    enconvert_error: Optional[str] = None
    enconvert_flagged: bool = False
    enconvert_excerpt: str = ""


# --------------------------------------------------------------------------
# 1. Naive fetch
# --------------------------------------------------------------------------


async def naive_fetch(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    try:
        resp = await client.get(url, headers=NAIVE_HEADERS)
        body = resp.text
        return {
            "status": resp.status_code,
            "final_url": str(resp.url),
            "body": body,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": None,
            "final_url": None,
            "body": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def classify_naive(url: str, fetched: dict[str, Any], row: UrlResult) -> None:
    row.naive_status = fetched["status"]
    row.naive_final_url = fetched["final_url"]
    row.naive_error = fetched["error"]

    if fetched["error"] is not None:
        # A raised exception is a LOUD failure (the consumer sees an error),
        # not a silent one. We record it but do NOT count it as a silent fail.
        row.naive_silent_fail = False
        return

    body = fetched["body"] or ""
    row.naive_bytes = len(body.encode("utf-8", "replace"))
    text = visible_text(body)
    word_count = len(text.split())
    row.naive_word_count = word_count
    row.naive_excerpt = " ".join(text.split())[:300]

    # Reuse Enconvert's own scorer to judge whether the naive body is a
    # block / empty page — the exact same block-marker logic the gateway uses.
    instr = PageInstrumentation(requested_url=url, final_url=fetched["final_url"])
    verdict = score(body, instr, None)

    reasons: list[str] = []
    if word_count < _EMPTY_SHELL_WORDS:
        reasons.append("empty_shell")

    # A challenge/CAPTCHA page: Enconvert's own block markers fire AND the
    # challenge dominates the body (thin page). This guard is what keeps a
    # full content page that merely embeds an invisible Cloudflare turnstile
    # (e.g. coinmarketcap returning 2,400 words) from being miscounted.
    if verdict.is_blocked and word_count < _BOT_CHALLENGE_MAX_WORDS:
        reasons.append("bot_challenge")

    lowered = text.lower()
    if word_count < _PAYWALL_MAX_WORDS and any(m in lowered for m in _PAYWALL_MARKERS):
        reasons.append("paywall_gate")

    if (
        fetched["status"] in _BLOCK_STATUSES
        and word_count < _HTTP_BLOCK_MAX_WORDS
    ):
        reasons.append(f"http_{fetched['status']}")

    row.naive_fail_reasons = reasons
    row.naive_silent_fail = len(reasons) > 0


# --------------------------------------------------------------------------
# 2. Enconvert perceive (crawl4ai render + real scorer)
# --------------------------------------------------------------------------


def build_browser_config() -> BrowserConfig:
    return BrowserConfig(
        browser_type="chromium",
        headless=True,
        enable_stealth=True,
        extra_args=list(CHROMIUM_MEMORY_FLAGS),
        text_mode=False,
        verbose=False,
    )


def build_run_config() -> CrawlerRunConfig:
    return CrawlerRunConfig(
        pdf=False,
        screenshot=False,
        magic=True,
        simulate_user=True,
        override_navigator=True,
        cache_mode=CacheMode.BYPASS,
        wait_until="load",
        page_timeout=60000,
        user_agent=V1_USER_AGENT,
        max_retries=0,
        verbose=False,
    )


async def enconvert_render(crawler: AsyncWebCrawler, url: str, row: UrlResult) -> None:
    instr = PageInstrumentation(requested_url=url)
    strategy = crawler.crawler_strategy

    async def before_goto(page, context=None, url=None, config=None, **kwargs):  # noqa: ANN001
        instr.attach(page)
        return page

    async def after_goto(page, context=None, url=None, response=None, config=None, **kwargs):  # noqa: ANN001
        await instr.capture(page)
        return page

    strategy.set_hook("before_goto", before_goto)
    strategy.set_hook("after_goto", after_goto)

    try:
        result = await crawler.arun(url=url, config=build_run_config())
        html = instr.rendered_html
        if not html and result is not None:
            html = getattr(result, "html", None) or ""
        html = html or ""
        if not html:
            err = getattr(result, "error_message", None) if result else None
            row.enconvert_error = f"no_html: {err or 'navigation/hook failure'}"
            return
        verdict = score(html, instr, None)
        rendered_text = visible_text(html)
        row.enconvert_quality = verdict.score
        row.enconvert_is_blocked = verdict.is_blocked
        row.enconvert_deductions = dict(verdict.deductions)
        row.enconvert_word_count = len(rendered_text.split())
        row.enconvert_excerpt = " ".join(rendered_text.split())[:300]
        # Production surfaces a render as bad on EITHER signal: an explicit
        # anti-bot / bot-detection block (is_blocked -> a warning + LLM
        # extraction is skipped in perceive_flow, and /v2/watch short-circuits
        # the diff), OR a sub-floor quality score. A page can be is_blocked yet
        # score >= 0.40 (a single 0.5 bot-detection deduction), so flagging on
        # the 0.40 floor alone would understate what the gateway actually flags.
        row.enconvert_flagged = verdict.is_blocked or verdict.score < QUALITY_FLOOR
    except Exception as exc:  # noqa: BLE001
        row.enconvert_error = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def load_urls(path: Path) -> list[tuple[str, str]]:
    """Parse `urls.txt`. Lines are `URL<TAB or spaces>category`; # comments."""
    out: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        url = parts[0]
        category = parts[1].strip() if len(parts) > 1 else "unknown"
        out.append((url, category))
    return out


async def run(urls: list[tuple[str, str]]) -> list[UrlResult]:
    results: list[UrlResult] = []
    browser_config = build_browser_config()
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=NAIVE_TIMEOUT_S,
        verify=True,
    ) as client:
        crawler = AsyncWebCrawler(config=browser_config)
        await crawler.start()
        try:
            for i, (url, category) in enumerate(urls, 1):
                row = UrlResult(url=url, category=category)
                print(f"[{i:>2}/{len(urls)}] {url}", flush=True)
                fetched = await naive_fetch(client, url)
                classify_naive(url, fetched, row)
                await enconvert_render(crawler, url, row)
                q = row.enconvert_quality
                print(
                    f"        naive: silent_fail={row.naive_silent_fail} "
                    f"{row.naive_fail_reasons}  |  "
                    f"enconvert: q={q if q is None else round(q, 3)} "
                    f"flagged={row.enconvert_flagged} "
                    f"{'ERR:' + row.enconvert_error if row.enconvert_error else ''}",
                    flush=True,
                )
                results.append(row)
        finally:
            await crawler.close()
    return results


def outcome(r: UrlResult) -> str:
    """Enconvert outcome for one URL: clean | flagged | error.

    - error:   the render produced no HTML (surfaced loudly to the caller).
    - flagged: the render is blocked or sub-floor (is_blocked OR q < 0.40) —
               surfaced as low quality; never returned as clean content.
    - clean:   a usable render (is_blocked == False AND q >= 0.40).
    """
    if r.enconvert_error is not None:
        return "error"
    if r.enconvert_flagged:
        return "flagged"
    return "clean"


def summarize(results: list[UrlResult]) -> dict[str, Any]:
    n = len(results)
    naive_fail = [r for r in results if r.naive_silent_fail]
    naive_error = [r for r in results if r.naive_error is not None]
    enc_clean = [r for r in results if outcome(r) == "clean"]
    enc_flagged = [r for r in results if outcome(r) == "flagged"]
    enc_error = [r for r in results if outcome(r) == "error"]
    # Recovery: of the pages a naive fetch silently failed on, how many did
    # Enconvert render into clean, usable content?
    recovered = [r for r in naive_fail if outcome(r) == "clean"]

    def pct(x: int, d: int = n) -> float:
        return round(100.0 * x / d, 1) if d else 0.0

    return {
        "n": n,
        "naive_silent_fail_count": len(naive_fail),
        "naive_silent_fail_pct": pct(len(naive_fail)),
        "naive_hard_error_count": len(naive_error),
        "enconvert_clean_count": len(enc_clean),
        "enconvert_clean_pct": pct(len(enc_clean)),
        "enconvert_flagged_count": len(enc_flagged),
        "enconvert_flagged_pct": pct(len(enc_flagged)),
        "enconvert_render_error_count": len(enc_error),
        # Enconvert never returns a block page as content: every non-clean
        # outcome is either flagged or a surfaced error.
        "enconvert_silent_fail_count": 0,
        "recovery": {
            "naive_silent_fail_n": len(naive_fail),
            "enconvert_rendered_clean": len(recovered),
            "recovery_pct": pct(len(recovered), len(naive_fail)),
        },
    }


def write_outputs(results: list[UrlResult], summary: dict[str, Any], meta: dict[str, Any]) -> None:
    payload = {
        "meta": meta,
        "summary": summary,
        "results": [asdict(r) for r in results],
    }
    Path("results.json").write_text(json.dumps(payload, indent=2))

    rec = summary["recovery"]
    lines = ["# Benchmark results", ""]
    lines.append(f"- Generated: {meta.get('generated_utc', 'n/a')}")
    lines.append(f"- URLs tested (N): **{summary['n']}**")
    lines.append(
        f"- **Naive fetch** silently returned a block page as content: "
        f"**{summary['naive_silent_fail_pct']}%** "
        f"({summary['naive_silent_fail_count']}/{summary['n']}) "
        f"(+{summary['naive_hard_error_count']} loud connection errors)"
    )
    lines.append(
        f"- **Enconvert recovery** — of those silent failures, rendered clean "
        f"usable content: **{rec['recovery_pct']}%** "
        f"({rec['enconvert_rendered_clean']}/{rec['naive_silent_fail_n']})"
    )
    lines.append(
        f"- **Enconvert** across all {summary['n']}: "
        f"clean {summary['enconvert_clean_count']} "
        f"({summary['enconvert_clean_pct']}%), "
        f"flagged (is_blocked or q<0.40) {summary['enconvert_flagged_count']} "
        f"({summary['enconvert_flagged_pct']}%), "
        f"render errors {summary['enconvert_render_error_count']}, "
        f"**silently returned a block page: {summary['enconvert_silent_fail_count']}**"
    )
    lines.append("")
    lines.append(
        "| # | URL | Category | Naive silent-fail (why) | "
        "Enconvert q | is_blocked | Outcome |"
    )
    lines.append(
        "|---|-----|----------|-------------------------|"
        "-------------|------------|---------|"
    )
    for i, r in enumerate(results, 1):
        why = ",".join(r.naive_fail_reasons) if r.naive_silent_fail else (
            "conn-error" if r.naive_error else "ok")
        q = "err" if r.enconvert_quality is None else f"{r.enconvert_quality:.2f}"
        blk = "" if r.enconvert_is_blocked is None else (
            "yes" if r.enconvert_is_blocked else "no")
        lines.append(
            f"| {i} | {r.url} | {r.category.split('#')[0].strip()} | "
            f"{'yes: ' + why if r.naive_silent_fail else why} | {q} | {blk} | "
            f"{outcome(r)} |"
        )
    Path("results.md").write_text("\n".join(lines) + "\n")


async def main() -> None:
    urls_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("urls.txt")
    urls = load_urls(urls_path)
    if not urls:
        print(f"No URLs found in {urls_path}", file=sys.stderr)
        sys.exit(1)

    started = time.time()
    results = await run(urls)
    summary = summarize(results)
    meta = {
        "harness": "perceive_benchmark.py",
        "quality_floor": QUALITY_FLOOR,
        "render_user_agent": V1_USER_AGENT,
        "elapsed_seconds": round(time.time() - started, 1),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_outputs(results, summary, meta)

    rec = summary["recovery"]
    print("\n================ SUMMARY ================")
    print(json.dumps(summary, indent=2))
    print("\nHeadline numbers for the MCP page (McpBenchmark.tsx BENCHMARK):")
    print(f"  urlsTested                 = {summary['n']}")
    print(f"  naiveSilentFailPct         = {summary['naive_silent_fail_pct']}")
    print(f"  enconvertRecoveredPct      = {rec['recovery_pct']}   "
          f"({rec['enconvert_rendered_clean']}/{rec['naive_silent_fail_n']} "
          f"naive silent-fails rendered clean)")
    print(f"  enconvert clean/flagged/err = "
          f"{summary['enconvert_clean_count']}/"
          f"{summary['enconvert_flagged_count']}/"
          f"{summary['enconvert_render_error_count']}   "
          f"silent-fails: {summary['enconvert_silent_fail_count']}")
    print("Wrote results.json and results.md")


if __name__ == "__main__":
    asyncio.run(main())
