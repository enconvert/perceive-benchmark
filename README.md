# The silent-failure benchmark

**perceive vs. a naive `fetch()`, across a fixed public set of bot-gated, paywalled, and JS-heavy URLs.**

When you point a naive HTTP client at a hostile page, the server often hands
back a *block page* — a Cloudflare "Just a moment" interstitial, a CAPTCHA, an
`Access Denied`, an empty JavaScript shell, or a paywall gate. A naive scraper
stores that response as if it were the content. It fails **silently**: HTTP 200,
looks fine, is garbage.

This repo measures how often that happens, and compares it to
[Enconvert](https://enconvert.com)'s `perceive` endpoint, which renders each
page in a real headless browser and scores the render with an 8-point
render-quality scorer. Enconvert never silently returns a block page as
content: it either renders the real page, or it flags the render as low quality
(`render_quality < 0.40`).

The URL list and the harness are public so you can run it yourself.

## What it measures

For each URL, the harness runs two arms:

1. **Naive fetch** — a single HTTP `GET` with a real Chrome User-Agent and
   normal `Accept` headers (no JavaScript). We give the naive path its *best
   shot* on purpose: a browser UA, not `python-requests`, so we measure the
   JS/bot-gate barrier, not UA-string blocking. A naive fetch **silently fails**
   when the returned body is any of:
   - an **anti-bot / CAPTCHA challenge** (detected by the same block markers
     Enconvert's scorer uses),
   - an **empty JS shell** (fewer than 20 visible words),
   - a **paywall / login gate** (an unambiguous gate phrase on a thin page),
   - an **HTTP refusal with a body** (401 / 403 / 429 / 451 / 503) that a naive
     `resp.text` consumer would ingest anyway.

   A raised connection error (timeout, reset) is counted separately as a *loud*
   failure — not a silent one — because the consumer at least sees an error.

2. **Enconvert perceive** — the page is rendered in a real headless browser via
   [crawl4ai](https://github.com/unclecode/crawl4ai) using the **exact**
   `BrowserConfig` + `CrawlerRunConfig` the Enconvert gateway runs in production
   (Chromium, stealth, `magic`, `simulate_user`, `override_navigator`,
   `wait_until="load"`), then scored by Enconvert's 8-point render-quality
   scorer. A render is **flagged** when `render_quality < 0.40`.

`scorer.py` and `instrumentation.py` are vendored **verbatim** from the
Enconvert gateway — the same code that produces the `render_quality` value
returned by `/v2/perceive` in production. The scorer is a pure function: eight
deductions (anti-bot challenge, bot detection, login wall, empty body, JS
errors, failed subresources, cross-domain redirect, slow render) subtracted from
`1.0` and clamped to `[0, 1]`. See `scorer.py` for the exact deduction table.

## Results

Run of **2026-07-07** (`results.json`), from a residential IP in India. Bot-management
verdicts move with IP reputation and geography — your run will differ; re-run it.

| Metric | Value |
|--------|-------|
| **URLs tested (N)** | **50** — 18 bot-gated, 15 paywalled, 17 JS-heavy |
| **Naive fetch silently returned a block page as content** | **52%** (26/50) |
| **Enconvert recovered** — rendered the real page where the naive fetch silently failed | **65%** (17/26) |
| **Enconvert flag-correctness** — of the pages it still could not render, how many it flagged | **100%** (11/11) |
| **Enconvert silently returned a block page** | **0** |

Two things worth stating plainly:

- **Enconvert is better at getting the real page.** On 26 URLs the naive fetch handed
  back a Cloudflare/DataDome challenge, an empty JS shell, or a paywall gate. Enconvert
  rendered the real page on 17 of them (65%) — including bypassing bot-management on
  `ft.com`, `economist.com`, `indeed.com`, `nike.com`, `glassdoor.com`, `dexscreener.com`,
  and executing the JS on SPAs (`spotify`, `aave`, `uniswap`, `excalidraw`) that a naive
  fetch returns blank.
- **When it can't, it says so.** On the other 11 (8 empty stubs like `g2.com`/`wsj.com`
  where even a headless browser got a title-only page, plus `zillow`'s CloudFront block,
  `bloomberg`'s "Are you a robot?" page, and one render timeout), Enconvert flagged every
  single one via `render_quality < 0.40` or `is_blocked` — or surfaced a render error. It
  never returned a block page as if it were content. **Precision and recall were both
  100%** on this set: every flag is a genuine empty/block (no real content page
  over-flagged — verify `coinmarketcap.com`, which renders 3,000+ words and is *not*
  flagged), and no genuinely-blocked render slipped through as "clean".

The naive silent-fail number is deliberately **conservative**: a page counts as a silent
failure only on a clear signal (anti-bot markers, an empty shell under 20 words, an
unambiguous paywall gate on a thin page, or a 401/403/429/451/503 with a body). Loud
connection errors (timeouts, resets) are counted separately, not as silent failures.

Every per-URL result — HTTP status, word counts, matched markers, the exact Enconvert
`render_quality` and per-deduction breakdown, and a rendered-text excerpt — is in
[`results.json`](results.json). A readable table is in [`results.md`](results.md).

## Reproduce it

```bash
pip install -r requirements.txt
playwright install chromium
python perceive_benchmark.py urls.txt
```

Outputs `results.json` (full per-URL data: status, word counts, matched
markers, per-deduction breakdown) and `results.md` (a summary table). The run
prints the three headline numbers at the end.

Requires Python 3.11+.

## Honest caveats

- **Results vary by IP reputation and geography.** Bot-management verdicts
  depend on the requesting IP's reputation and location. A clean residential IP
  may pass where a flagged datacenter IP is challenged, and vice-versa. Re-run
  it from your own network — the numbers will move, and that variance is itself
  the point.
- **Metered paywalls can beat the naive arm.** Some metered news sites ship the
  full article text in the initial HTML and enforce the gate later in
  client-side JS. For those, a naive fetch *wins* — and we count it as a win.
  Only hard gates and empty shells count as silent failures.
- **The naive arm is deliberately generous.** Full browser headers, redirects
  followed, a 25s timeout. We are not sandbagging it.
- **`perceive` renders; it does not defeat every wall.** On sites that block or
  gate even a real browser, `perceive` produces a low `render_quality` and
  **flags** it. That is the product: it tells you the render is bad instead of
  silently handing you a block page.

## Files

| File | What |
|------|------|
| `perceive_benchmark.py` | The harness (naive fetch + crawl4ai render + scorer) |
| `scorer.py` | Enconvert's render-quality scorer (vendored verbatim from the gateway) |
| `instrumentation.py` | Playwright render instrumentation (vendored verbatim) |
| `urls.txt` | The fixed 50-URL test set, with category + vendor notes |
| `results.json` | Full per-URL results from the published run |
| `results.md` | Human-readable summary table |
| `analyze.py` | Ground-truth review helper (per-URL precision/recall dump) |

## License

MIT. See `LICENSE`.
