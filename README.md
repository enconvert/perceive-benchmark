# The silent-failure benchmark (v2)

**Six ways to read a hostile public web page, one fixed corpus, one question:
when the site handed back a block page, did the tool say so?**

Point a scraper at a Cloudflare-protected page and the server often answers
`200 OK` with a "Just a moment..." interstitial, a CAPTCHA, an `Access Denied`
stub, an empty JavaScript shell or a login gate. Most tools store that body
as if it were the article. They fail **silently**: HTTP 200, looks fine, is
garbage. An AI agent downstream then reasons over garbage.

This repo measures how often that happens, per tool, and whether the tool
labelled the failure. It is the artifact behind the Show HN post
"Every scraper hands your agent the Cloudflare page with a 200. Ours labels
it." Everything here is public so you can re-run it and disagree with numbers.

## The two headline numbers

| Corpus | What it is | Status |
|--------|------------|--------|
| **legacy-50** | The original 50 URLs of the 2026-07-07 v1 run (18 bot-gated, 15 paywalled, 17 JS-heavy). Kept byte-for-byte so v1 numbers stay reproducible. | v1 run: naive fetch silently returned a block page on **52%** (26/50); the crawl4ai-direct arm returned **0** block pages as content and recovered 17/26 (65%). Archived in `results/2026-07-07-in-legacy-v1.md`. |
| **v2-200** | 200 URLs: the legacy 50 plus 150 more across four categories (bot-gated, paywalled, js-heavy, control), run against all six arms. | First weekly run pending; `results.md` is regenerated from `results/` on every run. |

The v1 harness counted a 401/403/429/451/503 with a block body as a naive
silent failure. v2 counts only 2xx bodies (see Definitions), so the v2
legacy-50 naive number will be lower than 52% by construction. To reproduce
the v1 figure exactly: `git checkout 9a15e53 && python perceive_benchmark.py urls.txt`.

## Arms

Each arm is asked for each URL once per run. Every arm is optional; a
missing key skips the arm and the skip is recorded in the results.

| Arm | What it does | Key | Cost unit recorded |
|-----|--------------|-----|--------------------|
| `naive` | `httpx` GET with a full Chrome header set, redirects followed, no JavaScript. Deliberately generous: a browser UA, not `python-requests`, so the barrier measured is the JS/bot gate, not UA blocking. | none | requests |
| `crawl4ai` | [crawl4ai](https://github.com/unclecode/crawl4ai) 0.8.9 open source, run directly: plain headless Chromium with stealth, `magic`, `simulate_user`, `override_navigator`, 60 s page timeout. No TLS rung, no escalation, no proxies. This is what self-hosting the open-source engine gets you. | none (needs `playwright install chromium`) | renders |
| `enconvert` | `POST https://api.enconvert.com/v2/perceive` with `outputs: ["markdown"]`. Reads `render_quality`, `is_blocked`, `deductions`, `status_code`, `billed` from the response and fetches the markdown signed URL (no key) when present. A blocked read is a `200` with `is_blocked: true` and empty `outputs`. | `ENCONVERT_API_KEY` | ops (billed reads) |
| `firecrawl` | `POST https://api.firecrawl.dev/v2/scrape` with `formats: ["rawHtml","markdown"]`. `rawHtml` is scored locally; `metadata.statusCode` is recorded and fed to the scorer as the upstream status. | `FIRECRAWL_API_KEY` | credits (`metadata.creditsUsed`, else 1) |
| `jina` | `GET https://r.jina.ai/<url>` with `Accept: application/json`. Returns text, not HTML, so only the text checks of the scorer can fire (markers, word counts); title/h1/mount-node checks cannot. | `JINA_API_KEY` optional (20 RPM without) | tokens (`usage.tokens`) |
| `scrapingbee` | `GET https://app.scrapingbee.com/api/v1?render_js=true&url=...`. The HTML body is scored; `Spb-initial-status-code` is the upstream status, `Spb-cost` the credits. | `SCRAPINGBEE_API_KEY` | credits |

Costs are recorded in each vendor's own unit and never converted to money;
list prices change and you can do that multiplication yourself.

## Definitions (identical across arms)

Every body an arm returns **as content** is scored with `scorer.py`, EnConvert's
render-quality scorer, vendored byte-for-byte from the gateway
(`test_benchmark.py` hashes both copies). It is a pure function: eleven named
deductions subtracted from 1.0 and clamped to [0, 1]:

| Deduction | Weight | Fires when |
|-----------|-------:|------------|
| `anti_bot_challenge` | 0.60 | two or more Cloudflare interstitial fingerprints in the raw HTML |
| `bot_detection` | 0.50 | two visible WAF/CAPTCHA phrases, or one strong block signature on a page under 200 words |
| `login_wall` | 0.65 | repeated auth vocabulary on a thin page, or a visible password input under 350 words |
| `empty_body` | 0.70 / 0.15 | fewer than 20 / fewer than 100 visible words |
| `unhydrated_shell` | 0.70 | a framework mount node (`#root`, `#__next`, ...) with under 20 words on a page under 500 words |
| `js_errors` | 0.20 / 0.10 | more than 10 / more than 3 console errors (browser arms only) |
| `resource_failures` | 0.15 / 0.05 | more than 10 / more than 3 failed subresources (browser arms only) |
| `domain_redirect` | 0.10 | the registrable domain changed between request and final URL |
| `slow_render` | 0.10 / 0.05 | more than 30 s / more than 15 s to render (browser arms only) |
| `http_error` | 0.70 | the main document's status is 400 or higher |
| `soft_404` | 0.65 | the title or h1 *is* a 404 phrase and the page has under 250 distinct words |

From that verdict, per (URL, arm):

- **block body**: any of `anti_bot_challenge`, `bot_detection`, `login_wall`,
  `http_error`, `soft_404`, `unhydrated_shell` fired, or the hard `empty_body`
  tier (under 20 words) fired. The soft tier is thin content, not a block.
- **labelled**: the arm itself flagged the read. Only the hosted `enconvert`
  arm can: `is_blocked` true, or `render_quality < 0.40` (the gateway's own
  floor). No other arm returns a quality verdict, so for them this is always false.
- **silently returned a block page** (the miss every table counts): the arm
  answered **success (2xx)**, the body is a **block body**, and the read was
  **not labelled**. For the hosted arm this reduces to: `is_blocked` is false
  AND the body is blocked per the scorer. That is the honest test of the
  product: the scorer is the product's own, so a miss means the product
  contradicted itself.
- **clean**: success, not labelled, not a block body.
- **loud error**: no success answer (non-2xx from the arm, exception, timeout).
  A consumer at least sees an error, so it is not a silent failure.
- **recovered naive misses**: URLs where the naive fetch silently returned a
  block page and this arm read clean content.

Two fairness notes. Firecrawl and ScrapingBee do report the upstream status
code alongside a success response; it is recorded in the misses table. A
caller who reads it avoids the garbage, but the default `markdown` field
does not carry that warning, which is the point being measured. The hosted
arm's `status_code` is *not* fed back into the local scorer: the API already
turned it into an `http_error` deduction and a sub-floor `render_quality`, so
feeding it again would count a labelled read as a miss.

## Corpus

`urls.txt`: 200 public URLs, `URL  category  set  # note`. Homepages and
public article or docs pages only; nothing behind a login, nothing personal.
The category is the *expected* barrier from public bot-management footprints
and vendor writeups; the per-category table in `results.md` shows what
actually happened on a given run.

| Category | Count | Why it is in the corpus |
|----------|------:|-------------------------|
| `bot-gated` | 60 | Sites fronted by Cloudflare, Akamai, DataDome, HUMAN/PerimeterX, Imperva or Kasada. The core case: the block page is served with a 200 or a 403 and *looks* like a page. |
| `paywalled` | 50 | Hard walls, registration walls and metered walls with a bot edge. Measures whether a gate page (thin, auth vocabulary) is returned as the article. Metered sites that ship the full text to a first visit legitimately beat the naive arm and are counted as clean. |
| `js-heavy` | 47 | SPAs whose HTML is an empty mount node until JavaScript runs. Measures the empty-shell failure: no block, just nothing, returned as content. |
| `control` | 43 | Static or server-rendered public pages (standards bodies, docs, government, free news, Wikipedia). Every arm should read these clean; any miss here is a false positive of the scorer and is reported as such. |

The 50 legacy rows are tagged `legacy`; the other 150 are tagged `v2`.

## Running it

```bash
pip install -r requirements.txt
playwright install chromium                      # only the crawl4ai arm needs it

python perceive_benchmark.py urls.txt            # every arm whose key is set
python perceive_benchmark.py urls.txt --arms naive,crawl4ai --limit 3   # local smoke, no keys, no spend
python perceive_benchmark.py urls.txt --corpus legacy --region in --out results/2026-09-22-in.json

python analyze.py results.json                   # or: python analyze.py results/
python -m pytest -q                              # hash check + synthetic-run check, no network
```

Keys are read from `ENCONVERT_API_KEY`, `FIRECRAWL_API_KEY`, `JINA_API_KEY`,
`SCRAPINGBEE_API_KEY`. The harness writes the raw rows to `--out`
(default `results.json`) and then runs `analyze.py`, which folds a `summary`
block into that JSON and writes `results.md` with, per corpus: the headline
numbers, a per-arm table, a per-category table and a misses table listing
every URL where any arm silently returned a block page, with the deductions
that fired. Requires Python 3.11+.

Rate limits: URLs run sequentially and the HTTP arms for one URL run
concurrently, so each vendor sees about one request every few seconds. The
hosted arm's 30/minute free-plan limit is not reached.

## Weekly runs

`.github/workflows/weekly.yml` runs every Monday 03:00 UTC (and on demand)
on a matrix of `ubuntu-latest` (GitHub-hosted, recorded as region `gh-hosted`)
and `self-hosted-in` (a self-hosted runner in India, region `in`). Because bot
verdicts move with egress IP, the two legs are the comparison. The India leg
is only scheduled when the repository variable `HAS_IN_RUNNER` is `true`, so
the workflow degrades to one leg until that runner exists. Each leg commits
`results/<date>-<region>.json` and regenerates `results.md` from the newest
file per region.

## Ethics

- Public pages only: homepages and public articles or docs. No pages behind
  a login, no personal data, no account pages.
- One request per URL per arm per run, weekly. No retries beyond a vendor's
  own defaults.
- No CAPTCHA solving, no proxies, no residential IP pools, no cookie reuse
  between runs. When a site blocks a request, that verdict is recorded, not
  worked around.
- Vendors are called through their documented public APIs with their own
  default settings for a JS render.

## Honest caveats

- **Results vary by IP reputation and geography.** Re-run from your own
  network; the numbers will move, and that variance is itself the finding.
- **Metered paywalls can beat the naive arm.** Sites that ship the full
  article and enforce the wall in client-side JS are a win for the naive fetch
  and are counted as clean.
- **Text-only arms score with fewer signals.** Jina returns text and the hosted
  arm is scored on its markdown, so the structural checks (title/h1 soft-404,
  mount-node shell) cannot fire on those bodies. HTML arms get the full scorer.
- **The scorer is heuristic.** It is the same ~450 lines that produce
  `render_quality` in production, published MIT here. If it misfires on a
  control page, that shows up in the misses table as a false positive.
- **`enconvert` and `crawl4ai` share an engine.** EnConvert's ladder is built
  on crawl4ai; the difference the two arms measure is the hosted ladder
  (TLS-first rung, escalation, quality gate, labels) versus the open-source
  engine on its own.

## Files

| File | What |
|------|------|
| `perceive_benchmark.py` | The harness: six arms, per-arm timing and cost, writes the JSON rows |
| `analyze.py` | Summaries and `results.md`; owns the shared definitions (`is_block_body`, `ARMS`) |
| `scorer.py` | EnConvert's 11-deduction render-quality scorer, byte-identical to the gateway |
| `instrumentation.py` | Playwright render instrumentation the scorer consumes, byte-identical to the gateway |
| `utils/url_registrable.py`, `services/page_quality/` | Vendored helper and import shims so the byte-identical scorer imports resolve |
| `urls.txt` | The 200-URL corpus with category and set columns |
| `results/` | One JSON (and regenerated `results.md`) per weekly run; the v1 archive is `2026-07-07-in-legacy-v1.*` |
| `results.md` | Latest headline, per-arm, per-category and misses tables |
| `test_benchmark.py` | Hash check against the gateway copies, corpus shape, synthetic-run check of `analyze.py` |
| `.github/workflows/weekly.yml` | The weekly matrix run |

## License

MIT. See `LICENSE`.
