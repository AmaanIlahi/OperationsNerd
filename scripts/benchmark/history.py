"""
Permanent benchmark log: results/HISTORY.csv (append-only) and the
HISTORY.md generated from it.

HISTORY.csv is never rewritten: rows are only appended, and a run that is
already in it (same timestamp) is skipped. HISTORY.md is derived and is
regenerated from the CSV (newest first) plus results/NOTES.md, so edit those,
not the .md.

Column meanings
  pass_rate     passed task runs / all task runs
  pass_hat_k    share of (task, business) cells where ALL k runs passed
                (pass^k: the reliability measure; always <= pass_rate)
  median_seconds_per_task   median wall-clock seconds of one task run
  unintended_data_changes   records changed/lost/added that no task intended
  core_lines_changed        lines changed in core code (everything outside
                            scripts/benchmark/ and database files) during the
                            run; 0 means the state before and after was identical
  fail_*        failed task runs by first-pass category
"""

import csv
import json
import os
import statistics

CSV_NAME = "HISTORY.csv"
MD_NAME = "HISTORY.md"
NOTES_NAME = "NOTES.md"

COLUMNS = ["timestamp", "commit", "dirty_tree", "model", "runs", "pass_rate", "pass_hat_k",
           "median_seconds_per_task", "total_tokens", "est_cost_usd", "unintended_data_changes",
           "core_lines_changed", "fail_pack_authoring_gap", "fail_implementation_bug", "fail_true_break"]

CATEGORY_COLUMN = {"pack-authoring gap": "fail_pack_authoring_gap",
                   "implementation bug": "fail_implementation_bug",
                   "true break": "fail_true_break"}


def core_lines_changed(git_before: dict, git_after: dict) -> int:
    """0 if core code was identical before and after; else the lines in the
    after-state diff (tracked changes plus untracked core files)."""
    if (git_before.get("diff_stat"), git_before.get("status")) == (git_after.get("diff_stat"), git_after.get("status")):
        return 0
    return int(git_after.get("lines", 0))


def build_row(meta: dict, businesses: list, git_before: dict, git_after: dict, commit: str, dirty: bool) -> dict:
    tasks = [t for b in businesses for r in b["runs"] for t in r["tasks"]]
    cells = {}
    for b in businesses:
        for r in b["runs"]:
            for t in r["tasks"]:
                cells.setdefault((b["business"], t["task"]), []).append(t["passed"])
    failed = [t for t in tasks if not t["passed"]]
    row = {
        "timestamp": meta["timestamp"], "commit": commit, "dirty_tree": "yes" if dirty else "no",
        "model": meta["model"], "runs": meta["runs"],
        "pass_rate": f"{sum(t['passed'] for t in tasks) / len(tasks):.4f}" if tasks else "0",
        "pass_hat_k": f"{sum(all(v) for v in cells.values()) / len(cells):.4f}" if cells else "0",
        "median_seconds_per_task": f"{statistics.median([t['seconds'] for t in tasks]):.1f}" if tasks else "0",
        "total_tokens": sum(t["input_tokens"] + t["output_tokens"] for t in tasks),
        "est_cost_usd": f"{sum(t['cost_usd'] for t in tasks):.4f}",
        "unintended_data_changes": sum(t["unintended_record_changes"] for t in tasks),
        "core_lines_changed": core_lines_changed(git_before, git_after),
        "fail_pack_authoring_gap": 0, "fail_implementation_bug": 0, "fail_true_break": 0,
    }
    for t in failed:
        col = CATEGORY_COLUMN.get(t.get("classification"))
        if col:
            row[col] += 1
    return row


def read_rows(results_root: str) -> list[dict]:
    path = os.path.join(results_root, CSV_NAME)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_row(results_root: str, row: dict) -> bool:
    """Appends one row; never touches existing rows. False if already logged."""
    if any(r["timestamp"] == row["timestamp"] for r in read_rows(results_root)):
        return False
    path = os.path.join(results_root, CSV_NAME)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        if new_file:
            w.writeheader()
        w.writerow(row)
    return True


def _pct(x: str) -> str:
    return f"{float(x) * 100:.1f}%"


def write_markdown(results_root: str) -> str:
    rows = sorted(read_rows(results_root), key=lambda r: r["timestamp"], reverse=True)
    L = ["# Benchmark history", "",
         "Generated from `HISTORY.csv` (append-only) and `NOTES.md`; do not edit by hand. Newest first. "
         "Each run links to its full report.", "",
         "- **Pass** = passed task runs / all task runs. **pass^k** = share of (task, business) cells where all k runs passed.",
         "- **Cost** is an estimate from assumed per-token rates unless the run's `meta.json` says otherwise.",
         "- **Core lines** = lines of core code changed during the run (0 = identical before and after).",
         "- **Failures** = failed task runs: pack-authoring gap / implementation bug / true break (first-pass rule classification).",
         "", "| Run | Commit | Dirty | Model | k | Pass | pass^k | Median s | Tokens | Cost $ | Unintended data | Core lines | Failures (gap / bug / break) |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| [{r['timestamp']}]({r['timestamp']}/report.md) | `{r['commit'][:7]}` | {r['dirty_tree']} | {r['model']} | {r['runs']} | "
                 f"{_pct(r['pass_rate'])} | {_pct(r['pass_hat_k'])} | {r['median_seconds_per_task']} | {int(r['total_tokens']):,} | "
                 f"{r['est_cost_usd']} | {r['unintended_data_changes']} | {r['core_lines_changed']} | "
                 f"{r['fail_pack_authoring_gap']} / {r['fail_implementation_bug']} / {r['fail_true_break']} |")
    if not rows:
        L.append("| (no runs logged yet) | | | | | | | | | | | | |")
    notes_path = os.path.join(results_root, NOTES_NAME)
    if os.path.exists(notes_path):
        L += ["", "## Notes", "", open(notes_path, encoding="utf-8").read().strip()]
    text = "\n".join(L) + "\n"
    with open(os.path.join(results_root, MD_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return text
