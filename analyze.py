#!/usr/bin/env python3
"""Post-run analysis + ground-truth dump for the benchmark.

Prints, for hand review:
  - every naive silent-fail with reasons + excerpt (false-positive check),
  - every Enconvert render with is_blocked / q / word_count / excerpt
    (ground-truth: genuine block vs real content -> precision/recall),
  - the recovery metric and the clean/flagged/error breakdown.
Run: python analyze.py
"""
import json

d = json.load(open("results.json"))
rows = d["results"]
s = d["summary"]


def outcome(r):
    if r["enconvert_error"]:
        return "error"
    if r["enconvert_flagged"]:
        return "flagged"
    return "clean"


print("SUMMARY:", json.dumps(s, indent=2))

print("\n" + "=" * 100)
print("NAIVE SILENT-FAILS (false-positive check):")
print("=" * 100)
for r in rows:
    if r["naive_silent_fail"]:
        print(f"{r['url'][:38]:38} st={str(r['naive_status']):>4} "
              f"w={str(r['naive_word_count']):>4} {r['naive_fail_reasons']}")
        print(f"    naive: {r['naive_excerpt'][:110]!r}")

print("\n" + "=" * 100)
print("ENCONVERT RENDERS — GROUND TRUTH (is genuine block vs real content?):")
print("=" * 100)
for r in rows:
    oc = outcome(r)
    q = "ERR" if r["enconvert_quality"] is None else f"{r['enconvert_quality']:.2f}"
    blk = "" if r["enconvert_is_blocked"] is None else ("BLK" if r["enconvert_is_blocked"] else "ok")
    ded = ",".join(r["enconvert_deductions"].keys()) if r["enconvert_deductions"] else ""
    print(f"[{oc:7}] {r['url'][:36]:36} q={q:>4} {blk:3} w={str(r['enconvert_word_count']):>5} {ded}")
    if oc in ("flagged", "error") or (r["enconvert_word_count"] or 0) < 120:
        exc = r["enconvert_excerpt"][:120] if r["enconvert_excerpt"] else (r["enconvert_error"] or "")[:120]
        print(f"    render: {exc!r}")

print("\n" + "=" * 100)
naive_fail = [r for r in rows if r["naive_silent_fail"]]
recovered = [r for r in naive_fail if outcome(r) == "clean"]
print(f"RECOVERY: of {len(naive_fail)} naive silent-fails, Enconvert rendered clean "
      f"on {len(recovered)} = {round(100*len(recovered)/max(1,len(naive_fail)),1)}%")
print("  NOT recovered:")
for r in naive_fail:
    if outcome(r) != "clean":
        print(f"    {r['url'][:40]:40} -> {outcome(r)} "
              f"(q={r['enconvert_quality']}, err={bool(r['enconvert_error'])})")
