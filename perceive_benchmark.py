#!/usr/bin/env python3
"""EnConvert silent-failure benchmark, v2: six arms over a fixed public corpus.

Every arm is asked for the same URL once. Whatever body it returns AS
CONTENT is scored with EnConvert's own render-quality scorer (scorer.py,
vendored byte-for-byte from the gateway). An arm "silently returned a block
page" when it answered success (2xx) and that body is a block page, a login
gate, an error page or an empty shell, and the arm did not label it. Only the
hosted EnConvert arm can label a read (is_blocked / render_quality), so for
it the miss test is: is_blocked false AND the body is blocked per the scorer.

Arms (each optional; skipped with a note when its key is absent):

    naive        httpx GET, real Chrome headers, no JavaScript
    crawl4ai     crawl4ai 0.8.9 open-source, Chromium + stealth, direct
    enconvert    POST https://api.enconvert.com/v2/perceive      ENCONVERT_API_KEY
    firecrawl    POST https://api.firecrawl.dev/v2/scrape          FIRECRAWL_API_KEY
    jina         GET  https://r.jina.ai/<url>                      JINA_API_KEY (optional)
    scrapingbee  GET  https://app.scrapingbee.com/api/v1           SCRAPINGBEE_API_KEY

Usage:
    python perceive_benchmark.py urls.txt                 # every arm with a key
    python perceive_benchmark.py urls.txt --arms naive,crawl4ai --limit 3
    python perceive_benchmark.py urls.txt --corpus legacy --region in --out results/x.json

Writes the JSON rows, then calls analyze.py to fold in the summary and write
results.md. Definitions live in analyze.py; the README explains the method.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from bs4 import BeautifulSoup

import analyze
from instrumentation import PageInstrumentation
from scorer import score

SCHEMA_VERSION = 2
QUALITY_FLOOR = analyze.QUALITY_FLOOR

ENCONVERT_URL = "https://api.enconvert.com/v2/perceive"
FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"
JINA_URL = "https://r.jina.ai/"
SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1"

# A full modern-Chrome header set for the NAIVE fetch. The naive path gets
# its best shot on purpose (browser UA and Accept headers) so the benchmark
# measures the JS / bot-gate barrier, not UA-string blocking.
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
VENDOR_TIMEOUT_S = 120.0
# The hosted ladder may spend up to 240 s escalating engines.
ENCONVERT_TIMEOUT_S = 300.0

# crawl4ai arm: plain Chromium with stealth, the open-source engine on its
# own (no TLS rung, no escalation). Flags match the gateway's Chromium
# launch so the render is comparable to a self-hosted deployment.
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

_INVISIBLE_TAGS = ("script", "style", "noscript", "template")


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(_INVISIBLE_TAGS):
        tag.decompose()
    return soup.get_text(" ")


# --------------------------------------------------------------------------
# Data model: one record per (URL, arm)
# --------------------------------------------------------------------------


@dataclass
class ArmResult:
    arm: str
    skipped: Optional[str] = None      # why the arm did not run (missing key, not installed)
    ok: bool = False                   # the arm answered success with a body
    arm_status: Optional[int] = None   # HTTP status the arm itself answered with
    upstream_status: Optional[int] = None  # status the arm reports for the target document
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    cost: Optional[float] = None       # in the arm's own unit, never converted to money
    cost_unit: Optional[str] = None
    word_count: Optional[int] = None
    quality: Optional[float] = None    # local scorer on the body the arm returned
    deductions: dict[str, float] = field(default_factory=dict)
    reported_quality: Optional[float] = None    # hosted arm: what the API said
    reported_is_blocked: Optional[bool] = None
    reported_deductions: dict[str, float] = field(default_factory=dict)
    billed: Optional[bool] = None
    labelled: bool = False             # the arm flagged the read itself (hosted only)
    silent_block: bool = False         # ok, block body, not labelled
    excerpt: str = ""


def finish(res: ArmResult, body: str, instr: PageInstrumentation) -> None:
    """Score the returned body locally and derive the silent-block verdict."""
    text = " ".join(visible_text(body).split())
    res.word_count = len(text.split())
    res.excerpt = text[:300]
    verdict = score(body, instr, None)
    res.quality = verdict.score
    res.deductions = dict(verdict.deductions)
    res.silent_block = res.ok and not res.labelled and analyze.is_block_body(res.deductions)


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


# --------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------


async def arm_naive(client: httpx.AsyncClient, url: str) -> ArmResult:
    res = ArmResult("naive", cost_unit="requests", cost=1.0)
    try:
        resp = await client.get(url, headers=NAIVE_HEADERS, timeout=NAIVE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - any transport failure is a loud error
        res.error = _err(exc)
        return res
    res.arm_status = res.upstream_status = resp.status_code
    res.ok = 200 <= resp.status_code < 300
    if not res.ok:
        res.error = f"http_{resp.status_code}"
    finish(res, resp.text, PageInstrumentation(
        requested_url=url, final_url=str(resp.url), http_status=resp.status_code,
    ))
    return res


async def arm_crawl4ai(crawler: Any, url: str) -> ArmResult:
    from crawl4ai import CacheMode, CrawlerRunConfig

    res = ArmResult("crawl4ai", cost_unit="renders", cost=1.0)
    instr = PageInstrumentation(requested_url=url)
    strategy = crawler.crawler_strategy

    async def before_goto(page, context=None, url=None, config=None, **kwargs):  # noqa: ANN001
        instr.attach(page)
        return page

    async def after_goto(page, context=None, url=None, response=None, config=None, **kwargs):  # noqa: ANN001
        if response is not None:
            instr.http_status = response.status  # mirrors perceive_flow's after_goto
        await instr.capture(page)
        return page

    strategy.set_hook("before_goto", before_goto)
    strategy.set_hook("after_goto", after_goto)
    config = CrawlerRunConfig(
        magic=True, simulate_user=True, override_navigator=True,
        cache_mode=CacheMode.BYPASS, wait_until="load", page_timeout=60000,
        user_agent=V1_USER_AGENT, max_retries=0, verbose=False,
    )
    try:
        result = await crawler.arun(url=url, config=config)
    except Exception as exc:  # noqa: BLE001
        res.error = _err(exc)
        return res
    html = instr.rendered_html or getattr(result, "html", None) or ""
    res.upstream_status = instr.http_status or getattr(result, "status_code", None)
    res.arm_status = res.upstream_status
    if not html:
        res.error = f"no_html: {getattr(result, 'error_message', None) or 'navigation failure'}"
        return res
    # crawl4ai marks a >= 400 document as success=False (a loud error); the
    # upstream status is still fed to the scorer so a 2xx-wrapped error page
    # (behind a proxy or CDN) would count as masked.
    res.ok = bool(getattr(result, "success", True))
    if not res.ok:
        res.error = f"http_{res.upstream_status}: {str(getattr(result, 'error_message', ''))[:200]}"
    finish(res, html, instr)
    return res


async def arm_enconvert(client: httpx.AsyncClient, url: str, key: str) -> ArmResult:
    res = ArmResult("enconvert", cost_unit="ops")
    try:
        resp = await client.post(
            ENCONVERT_URL, json={"url": url, "outputs": ["markdown"]},
            headers={"X-API-Key": key}, timeout=ENCONVERT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        res.error = _err(exc)
        return res
    res.arm_status = resp.status_code
    if not 200 <= resp.status_code < 300:
        res.error = f"http_{resp.status_code}: {resp.text[:200]}"
        return res
    data = resp.json()
    res.ok = True
    res.reported_quality = data.get("render_quality")
    res.reported_is_blocked = data.get("is_blocked")
    res.reported_deductions = data.get("deductions") or {}
    res.upstream_status = data.get("status_code")
    res.billed = data.get("billed")
    res.cost = None if res.billed is None else float(res.billed)
    # The product's own label. A blocked read is a 200 with is_blocked true
    # and no outputs; a sub-floor render_quality is the other flag callers
    # gate on. Either one means the read was not returned as clean content.
    res.labelled = bool(res.reported_is_blocked) or (
        res.reported_quality is not None and res.reported_quality < QUALITY_FLOOR
    )
    body = ""
    md_url = ((data.get("outputs") or {}).get("markdown") or {}).get("url")
    if md_url:
        try:
            body = (await client.get(md_url, timeout=VENDOR_TIMEOUT_S)).text  # signed URL, no key
        except Exception as exc:  # noqa: BLE001
            res.error = f"artifact: {_err(exc)}"
    # The status code is not fed to the local scorer here: the API already
    # labels it (status_code + http_error deduction => render_quality 0.3).
    finish(res, body, PageInstrumentation(requested_url=url, final_url=data.get("url_final")))
    return res


async def arm_firecrawl(client: httpx.AsyncClient, url: str, key: str) -> ArmResult:
    res = ArmResult("firecrawl", cost_unit="credits")
    try:
        resp = await client.post(
            FIRECRAWL_URL, json={"url": url, "formats": ["rawHtml", "markdown"]},
            headers={"Authorization": f"Bearer {key}"}, timeout=VENDOR_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        res.error = _err(exc)
        return res
    res.arm_status = resp.status_code
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if not (200 <= resp.status_code < 300 and payload.get("success")):
        res.error = f"http_{resp.status_code}: {str(payload.get('error') or resp.text)[:200]}"
        return res
    data = payload.get("data") or {}
    meta = data.get("metadata") or {}
    res.ok = True
    res.upstream_status = meta.get("statusCode")
    # ponytail: Firecrawl bills one credit per basic scrape and states that
    # error-status pages are returned and charged; creditsUsed wins when present.
    res.cost = float(meta.get("creditsUsed", 1))
    finish(res, data.get("rawHtml") or data.get("markdown") or "", PageInstrumentation(
        requested_url=url, final_url=meta.get("url") or meta.get("sourceURL"),
        http_status=res.upstream_status,
    ))
    return res


async def arm_jina(client: httpx.AsyncClient, url: str, key: Optional[str]) -> ArmResult:
    res = ArmResult("jina", cost_unit="tokens")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        resp = await client.get(JINA_URL + url, headers=headers, timeout=VENDOR_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        res.error = _err(exc)
        return res
    res.arm_status = resp.status_code
    if not 200 <= resp.status_code < 300:
        res.error = f"http_{resp.status_code}: {resp.text[:200]}"
        return res
    try:
        data = resp.json().get("data") or {}
    except ValueError:
        data = {"content": resp.text}
    res.ok = True
    res.cost = (data.get("usage") or {}).get("tokens")
    # Jina returns text, not HTML, so the structural checks (title, h1,
    # mount nodes) cannot fire; marker and word-count checks still do.
    finish(res, data.get("content") or "", PageInstrumentation(
        requested_url=url, final_url=data.get("url"),
    ))
    return res


async def arm_scrapingbee(client: httpx.AsyncClient, url: str, key: str) -> ArmResult:
    res = ArmResult("scrapingbee", cost_unit="credits")
    try:
        resp = await client.get(
            SCRAPINGBEE_URL, params={"api_key": key, "url": url, "render_js": "true"},
            timeout=VENDOR_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        res.error = _err(exc)
        return res
    res.arm_status = resp.status_code
    initial = resp.headers.get("Spb-initial-status-code")
    res.upstream_status = int(initial) if initial and initial.isdigit() else resp.status_code
    cost = resp.headers.get("Spb-cost")
    res.cost = float(cost) if cost else None
    res.ok = 200 <= resp.status_code < 300
    if not res.ok:
        res.error = f"http_{resp.status_code}: {resp.text[:200]}"
    finish(res, resp.text, PageInstrumentation(
        requested_url=url, final_url=resp.headers.get("Spb-resolved-url"),
        http_status=res.upstream_status,
    ))
    return res


async def timed(fn: Callable[..., Awaitable[ArmResult]], *args: Any) -> ArmResult:
    started = time.monotonic()
    res = await fn(*args)
    res.elapsed_ms = int((time.monotonic() - started) * 1000)
    return res


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def load_urls(path: Path) -> list[tuple[str, str, str]]:
    """Parse urls.txt: ``URL  category  [legacy|v2]   # note``."""
    out: list[tuple[str, str, str]] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        url = parts[0]
        category = parts[1] if len(parts) > 1 else "unknown"
        corpus = parts[2] if len(parts) > 2 else "v2"
        out.append((url, category, corpus))
    return out


def scorer_sha256() -> str:
    return hashlib.sha256((Path(__file__).parent / "scorer.py").read_bytes()).hexdigest()


async def run(urls: list[tuple[str, str, str]], arms: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    keys = {
        "enconvert": os.getenv("ENCONVERT_API_KEY"),
        "firecrawl": os.getenv("FIRECRAWL_API_KEY"),
        "jina": os.getenv("JINA_API_KEY"),
        "scrapingbee": os.getenv("SCRAPINGBEE_API_KEY"),
    }
    skipped: dict[str, str] = {}
    for arm, env in (("enconvert", "ENCONVERT_API_KEY"), ("firecrawl", "FIRECRAWL_API_KEY"),
                     ("scrapingbee", "SCRAPINGBEE_API_KEY")):
        if arm in arms and not keys[arm]:
            skipped[arm] = f"no {env}"

    crawler = None
    if "crawl4ai" in arms:
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig
            crawler = AsyncWebCrawler(config=BrowserConfig(
                browser_type="chromium", headless=True, enable_stealth=True,
                extra_args=list(CHROMIUM_MEMORY_FLAGS), text_mode=False, verbose=False,
            ))
            await crawler.start()
        except Exception as exc:  # noqa: BLE001 - missing package or browser binary
            skipped["crawl4ai"] = f"crawl4ai unavailable: {_err(exc)}"
            crawler = None

    rows: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for i, (url, category, corpus) in enumerate(urls, 1):
                print(f"[{i:>3}/{len(urls)}] {url}", flush=True)
                tasks: dict[str, Awaitable[ArmResult]] = {}
                if "naive" in arms:
                    tasks["naive"] = timed(arm_naive, client, url)
                if "enconvert" in arms and keys["enconvert"]:
                    tasks["enconvert"] = timed(arm_enconvert, client, url, keys["enconvert"])
                if "firecrawl" in arms and keys["firecrawl"]:
                    tasks["firecrawl"] = timed(arm_firecrawl, client, url, keys["firecrawl"])
                if "jina" in arms:
                    tasks["jina"] = timed(arm_jina, client, url, keys["jina"])
                if "scrapingbee" in arms and keys["scrapingbee"]:
                    tasks["scrapingbee"] = timed(arm_scrapingbee, client, url, keys["scrapingbee"])
                # The HTTP arms are independent services; the single Chromium
                # runs afterwards so the render never competes for CPU.
                done = await asyncio.gather(*tasks.values())
                results: dict[str, ArmResult] = dict(zip(tasks, done))
                if crawler is not None:
                    results["crawl4ai"] = await timed(arm_crawl4ai, crawler, url)
                for arm, note in skipped.items():
                    results[arm] = ArmResult(arm, skipped=note)
                rows.append({
                    "url": url, "category": category, "set": corpus,
                    "arms": {arm: asdict(results[arm]) for arm in analyze.ARMS if arm in results},
                })
                print("      " + "  ".join(
                    f"{arm}=" + ("skip" if r.skipped else "ERR" if r.error and not r.ok else
                                 "SILENT" if r.silent_block else "labelled" if r.labelled else
                                 f"q{r.quality:.2f}" if r.quality is not None else "?")
                    for arm, r in results.items()
                ), flush=True)
    finally:
        if crawler is not None:
            await crawler.close()
    return rows, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("urls", nargs="?", default="urls.txt")
    parser.add_argument("--corpus", choices=("legacy", "v2"), default="v2",
                        help="legacy = the original 50 URLs only; v2 = all")
    parser.add_argument("--arms", default=",".join(analyze.ARMS),
                        help="comma-separated subset of " + ",".join(analyze.ARMS))
    parser.add_argument("--limit", type=int, default=0, help="first N URLs only (smoke runs)")
    parser.add_argument("--region", default=os.getenv("BENCH_REGION", "local"),
                        help="egress label recorded in meta and used by analyze.py")
    parser.add_argument("--out", default="results.json")
    args = parser.parse_args()

    arms = [a for a in args.arms.split(",") if a]
    unknown = set(arms) - set(analyze.ARMS)
    if unknown:
        sys.exit(f"unknown arms: {', '.join(sorted(unknown))}")
    urls = load_urls(Path(args.urls))
    if args.corpus == "legacy":
        urls = [u for u in urls if u[2] == "legacy"]
    if args.limit:
        urls = urls[: args.limit]
    if not urls:
        sys.exit(f"No URLs selected from {args.urls}")

    started = time.time()
    rows, skipped = asyncio.run(run(urls, arms))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "meta": {
            "schema": SCHEMA_VERSION,
            "harness": "perceive_benchmark.py",
            "region": args.region,
            "corpus": args.corpus,
            "arms_requested": arms,
            "arms_skipped": skipped,
            "quality_floor": QUALITY_FLOOR,
            "scorer_sha256": scorer_sha256(),
            "elapsed_seconds": round(time.time() - started, 1),
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "rows": rows,
    }, indent=1))
    analyze.main([str(out)])  # folds the summary into the JSON and writes ./results.md
    summary = json.loads(out.read_text())["summary"]
    print("\n================ HEADLINES ================")
    for label, s in summary.items():
        for arm in ("naive", "enconvert"):
            st = s["arms"].get(arm, {})
            if st.get("skipped"):
                print(f"  {label:<12} {arm:<10} skipped: {st['skipped']}")
            else:
                print(f"  {label:<12} {arm:<10} silent block pages {st['silent_block']}/{st['attempted']} "
                      f"({st['silent_block_pct']}%)  clean {st['clean']}  labelled {st['labelled']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
