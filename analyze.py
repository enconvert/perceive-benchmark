#!/usr/bin/env python3
"""Turn harness output into results.md and fold the summary back into the JSON.

Standard library only, so it runs anywhere (CI, a laptop without
Playwright). The harness imports the shared definitions from here.

    python analyze.py results.json              # one run -> results.md
    python analyze.py results/                  # newest run per region -> results.md
    python analyze.py results.json --out out.md

Definitions (see README "Definitions"):

* block body   -- the local scorer fired a block/gate/error deduction on the
                  body the arm returned as content (BLOCK_KEYS), or the hard
                  empty-body tier (< 20 visible words). The soft tier (20-99
                  words) is thin content, not a block.
* labelled     -- the arm itself flagged the read. Only the hosted EnConvert
                  arm can do this: is_blocked, or render_quality < 0.40.
* silent block -- the arm answered success (2xx) with a block body and did
                  not label it. This is the miss every table counts.
* clean        -- success, not labelled, not a block body.
* loud error   -- the arm did not answer success (non-2xx, exception, timeout).
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Optional

ARMS: tuple[str, ...] = (
    "naive", "crawl4ai", "enconvert", "firecrawl", "jina", "scrapingbee",
)
ARM_LABELS = {
    "naive": "Naive fetch (httpx, Chrome UA, no JS)",
    "crawl4ai": "crawl4ai OSS direct (Chromium, stealth)",
    "enconvert": "EnConvert hosted API (/v2/perceive)",
    "firecrawl": "Firecrawl (/v2/scrape)",
    "jina": "Jina Reader (r.jina.ai)",
    "scrapingbee": "ScrapingBee (render_js=true)",
}
QUALITY_FLOOR = 0.40
BLOCK_KEYS = frozenset({
    "anti_bot_challenge", "bot_detection", "login_wall",
    "http_error", "soft_404", "unhydrated_shell",
})
_EMPTY_HARD_MIN = 0.5  # the hard empty_body tier is 0.7; the soft tier 0.15


def is_block_body(deductions: dict[str, float]) -> bool:
    """True when the scored body is a block page, gate, error page or empty shell."""
    if BLOCK_KEYS & set(deductions):
        return True
    return deductions.get("empty_body", 0.0) >= _EMPTY_HARD_MIN


def is_clean(arm: Optional[dict[str, Any]]) -> bool:
    return bool(
        arm and not arm.get("skipped") and arm.get("ok")
        and not arm.get("labelled") and not is_block_body(arm.get("deductions") or {})
    )


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def summarize(rows: list[dict[str, Any]], with_categories: bool = True) -> dict[str, Any]:
    """Per-arm counts for one corpus (a list of harness rows)."""
    naive_misses = {
        r["url"] for r in rows if (r["arms"].get("naive") or {}).get("silent_block")
    }
    arms: dict[str, Any] = {}
    for arm in ARMS:
        recs = [r["arms"][arm] for r in rows if arm in r["arms"]]
        ran = [a for a in recs if not a.get("skipped")]
        if not ran:
            note = next((a["skipped"] for a in recs if a.get("skipped")), "not run")
            arms[arm] = {"skipped": note}
            continue
        ok = [a for a in ran if a.get("ok")]
        silent = [a for a in ran if a.get("silent_block")]
        labelled = [a for a in ran if a.get("labelled")]
        clean = [a for a in ran if is_clean(a)]
        times = [a["elapsed_ms"] for a in ran if a.get("elapsed_ms") is not None]
        costs = [a["cost"] for a in ran if a.get("cost") is not None]
        arms[arm] = {
            "attempted": len(ran),
            "ok": len(ok),
            "clean": len(clean),
            "silent_block": len(silent),
            "silent_block_pct": _pct(len(silent), len(ran)),
            "labelled": len(labelled),
            "loud_error": len(ran) - len(ok),
            "recovered_naive_misses": sum(
                1 for r in rows
                if r["url"] in naive_misses and is_clean(r["arms"].get(arm))
            ),
            "median_ms": int(statistics.median(times)) if times else None,
            "cost": round(sum(costs), 2) if costs else None,
            "cost_unit": ran[0].get("cost_unit"),
            "billed": sum(1 for a in ran if a.get("billed")) if arm == "enconvert" else None,
        }
    out: dict[str, Any] = {"n": len(rows), "naive_misses": len(naive_misses), "arms": arms}
    if not with_categories:
        return out
    out["by_category"] = {
        cat: {
            "n": sum(1 for r in rows if r["category"] == cat),
            "arms": summarize(
                [r for r in rows if r["category"] == cat], with_categories=False
            )["arms"],
        }
        for cat in sorted({r["category"] for r in rows})
    }
    out["misses"] = [
        {
            "url": r["url"], "category": r["category"], "arm": arm,
            "deductions": a.get("deductions") or {},
            "upstream_status": a.get("upstream_status"),
            "arm_status": a.get("arm_status"),
            "excerpt": (a.get("excerpt") or "")[:120],
        }
        for r in rows for arm, a in r["arms"].items() if a.get("silent_block")
    ]
    return out


def corpora(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The two headline corpora: the legacy 50 and everything (v2)."""
    legacy = [r for r in rows if r.get("set") == "legacy"]
    out: dict[str, list[dict[str, Any]]] = {}
    if legacy:
        out[f"legacy-{len(legacy)}"] = legacy
    if len(rows) != len(legacy):
        out[f"v2-{len(rows)}"] = rows
    return out


# --- Markdown -----------------------------------------------------------------


def _fmt_s(ms: Optional[int]) -> str:
    return "" if ms is None else f"{ms / 1000:.1f}"


def _fmt_cost(stats: dict[str, Any]) -> str:
    if stats.get("cost") is None:
        return ""
    unit = stats.get("cost_unit") or ""
    billed = stats.get("billed")
    extra = f" ({billed} billed)" if billed is not None else ""
    return f"{stats['cost']:g} {unit}{extra}".strip()


def render_section(meta: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    ran = [a for a in ARMS if any(not (r["arms"].get(a) or {"skipped": 1}).get("skipped") for r in rows)]
    lines = [
        f"## Run {meta.get('generated_utc', '?')[:10]} from `{meta.get('region', 'unknown')}`",
        "",
        f"- Harness `{meta.get('harness', '?')}` schema {meta.get('schema', '?')}, "
        f"scorer sha256 `{(meta.get('scorer_sha256') or '')[:12]}`, "
        f"arms run: {', '.join(ran) or 'none'}",
    ]
    for arm in ARMS:
        if arm not in ran:
            note = next(((r["arms"].get(arm) or {}).get("skipped") for r in rows if r["arms"].get(arm)), None)
            if note:
                lines.append(f"- `{arm}` skipped: {note}")
    lines.append("")
    for label, sub in corpora(rows).items():
        s = summarize(sub)
        lines += render_corpus(label, s)
    return lines


def render_corpus(label: str, s: dict[str, Any]) -> list[str]:
    arms = s["arms"]
    naive = arms.get("naive", {})
    enc = arms.get("enconvert", {})
    lines = [f"### Headline: {label} (N={s['n']})", ""]
    if naive.get("skipped"):
        lines.append(f"- Naive fetch: skipped ({naive['skipped']})")
    else:
        lines.append(
            f"- **Naive fetch silently returned a block page as content: "
            f"{naive['silent_block_pct']}%** ({naive['silent_block']}/{naive['attempted']})"
        )
    if enc.get("skipped"):
        lines.append(f"- EnConvert hosted API: skipped ({enc['skipped']})")
    else:
        lines.append(
            f"- **EnConvert hosted API silently returned a block page: "
            f"{enc['silent_block']}** ({enc['silent_block_pct']}% of {enc['attempted']}); "
            f"labelled {enc['labelled']} (is_blocked or render_quality < {QUALITY_FLOOR}), "
            f"clean {enc['clean']}, loud errors {enc['loud_error']}, "
            f"recovered {enc['recovered_naive_misses']}/{s['naive_misses']} naive misses"
        )
    lines += ["", f"#### Per arm ({label})", "",
              "| Arm | Attempted | Clean | Silent block page | Labelled | Loud error | "
              "Recovered naive misses | Median s | Cost |",
              "|-----|----------:|------:|------------------:|---------:|-----------:|"
              "-----------------------:|---------:|------|"]
    for arm in ARMS:
        st = arms.get(arm, {})
        if st.get("skipped"):
            lines.append(f"| {ARM_LABELS[arm]} | skipped: {st['skipped']} | | | | | | | |")
            continue
        lines.append(
            f"| {ARM_LABELS[arm]} | {st['attempted']} | {st['clean']} | "
            f"{st['silent_block']} ({st['silent_block_pct']}%) | {st['labelled']} | "
            f"{st['loud_error']} | {st['recovered_naive_misses']} | "
            f"{_fmt_s(st['median_ms'])} | {_fmt_cost(st)} |"
        )
    lines += ["", f"#### Per category ({label}): silent block pages / attempted", "",
              "| Category | N | " + " | ".join(ARMS) + " |",
              "|----------|--:|" + "|".join("---:" for _ in ARMS) + "|"]
    for cat, c in s["by_category"].items():
        cells = []
        for arm in ARMS:
            st = c["arms"].get(arm, {})
            cells.append("skipped" if st.get("skipped") else f"{st['silent_block']}/{st['attempted']}")
        lines.append(f"| {cat} | {c['n']} | " + " | ".join(cells) + " |")
    lines += ["", f"#### Misses ({label}): every URL where an arm silently returned a block page", ""]
    if not s["misses"]:
        lines.append("None.")
    else:
        lines += ["| URL | Category | Arm | Deductions fired | Upstream status | Arm status |",
                  "|-----|----------|-----|------------------|----------------:|-----------:|"]
        for m in s["misses"]:
            ded = ", ".join(f"{k} {v:g}" for k, v in m["deductions"].items()) or "-"
            lines.append(
                f"| {m['url']} | {m['category']} | {m['arm']} | {ded} | "
                f"{m['upstream_status'] if m['upstream_status'] is not None else ''} | "
                f"{m['arm_status'] if m['arm_status'] is not None else ''} |"
            )
    lines.append("")
    return lines


def load_run(path: Path) -> Optional[dict[str, Any]]:
    data = json.loads(path.read_text())
    if (data.get("meta") or {}).get("schema") != 2:
        return None
    return data


def render_v1_archive(path: Path) -> list[str]:
    """Headline of the 2026-07-07 v1 run, whose JSON predates schema 2.

    v1 had two arms: the naive fetch and crawl4ai run directly with the
    gateway's Chromium config (labelled "Enconvert perceive" at the time);
    it counted 4xx block bodies as naive silent failures, which v2 does not.
    """
    data = json.loads(path.read_text())
    s = data.get("summary") or {}
    rec = s.get("recovery") or {}
    return [
        f"## Archived v1 run {(data.get('meta') or {}).get('generated_utc', '?')[:10]} "
        f"(legacy-{s.get('n', '?')}, schema 1, residential IP in India)",
        "",
        f"- **Naive fetch silently returned a block page as content: "
        f"{s.get('naive_silent_fail_pct', '?')}%** "
        f"({s.get('naive_silent_fail_count', '?')}/{s.get('n', '?')}), "
        "v1 definition (2xx block bodies plus 401/403/429/451/503 bodies)",
        f"- **crawl4ai direct (the v1 \"Enconvert perceive\" arm) silently returned a block page: "
        f"{s.get('enconvert_silent_fail_count', '?')}**; clean {s.get('enconvert_clean_count', '?')}, "
        f"flagged {s.get('enconvert_flagged_count', '?')}, render errors "
        f"{s.get('enconvert_render_error_count', '?')}; recovered "
        f"{rec.get('enconvert_rendered_clean', '?')}/{rec.get('naive_silent_fail_n', '?')} naive misses "
        f"({rec.get('recovery_pct', '?')}%)",
        f"- Per-URL table: `{path.with_suffix('.md')}`; 8-key scorer of that date, not the current 11-key one",
        "",
    ]


def newest_per_region(folder: Path) -> list[Path]:
    """Files are named <date>-<region>.json, so a sort picks the newest per region."""
    best: dict[str, Path] = {}
    for path in sorted(folder.glob("*.json")):
        run = load_run(path)
        if run is None:
            continue
        best[run["meta"].get("region", path.stem)] = path
    return list(best.values())


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", nargs="?", default="results.json",
                        help="a results JSON file or a folder of them")
    parser.add_argument("--out", default="results.md")
    args = parser.parse_args(argv)

    source = Path(args.source)
    paths = newest_per_region(source) if source.is_dir() else [source]
    lines = ["# Benchmark results", "",
             "Generated by `analyze.py`; definitions and method are in the README. "
             "Newest run per egress region first, archived v1 run last.", ""]
    v1_archives = sorted(source.glob("*-v1.json")) if source.is_dir() else []
    for path in paths:
        run = load_run(path)
        if run is None:
            if path not in v1_archives:
                lines.append(f"- `{path}` skipped: not a schema-2 results file")
            continue
        run["summary"] = {label: summarize(sub) for label, sub in corpora(run["rows"]).items()}
        path.write_text(json.dumps(run, indent=1))
        lines += render_section(run["meta"], run["rows"])
    for path in v1_archives:
        lines += render_v1_archive(path)
    Path(args.out).write_text("\n".join(lines).rstrip() + "\n")
    print(f"Wrote {args.out} from {', '.join(str(p) for p in paths + v1_archives)}")


if __name__ == "__main__":
    main()
