"""
costs.py — what runs have really cost, from the token counts saved on each run.

    uv run --no-project --with-requirements requirements.txt python costs.py          # last 30 days
    uv run --no-project --with-requirements requirements.txt python costs.py --days 7

On Railway, run it inside the service (railway ssh, then the same command) so it reads
the database on the volume. Prices are list prices per million tokens; check them on
Anthropic's pricing page before relying on the totals.
"""
import argparse
import time
from collections import defaultdict

from storage import Store

# $ per million tokens: input, output. Cache writes cost 1.25x input, cache reads 0.1x.
PRICES = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100K tokens
}
CACHE_WRITE, CACHE_READ = 1.25, 0.10

# Size bands for the summary, by rows in the file.
BANDS = [(1_000, "up to 1k rows"), (5_000, "1k-5k rows"), (30_000, "5k-30k rows")]


def run_cost(job: dict) -> float | None:
    """Dollars for one run, or None when its model has no price here (such as the scripted test agent)."""
    price = PRICES.get(job.get("model") or "")
    if not price:
        return None
    per_input, per_output = price[0] / 1e6, price[1] / 1e6
    return (job["input_tokens"] * per_input
            + job["cache_write_tokens"] * per_input * CACHE_WRITE
            + job["cache_read_tokens"] * per_input * CACHE_READ
            + job["output_tokens"] * per_output)


def band(rows: int) -> str:
    return next((name for limit, name in BANDS if rows <= limit), "over 30k rows")


def report(store: Store, days: float) -> str:
    jobs = [j for j in store.all_jobs(time.time() - days * 86400) if j["steps"]]
    if not jobs:
        return f"No runs with token counts in the last {days:g} days."

    lines = [f"{'Started':<17} {'Rows':>7} {'Mode':<7} {'Status':<11} {'Steps':>5} "
             f"{'Input':>9} {'Output':>8} {'Cost':>8}"]
    by_band = defaultdict(list)
    for job in jobs:
        cost = run_cost(job)
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(job["created_at"]))
        lines.append(f"{started:<17} {job['rows']:>7,} {job['mode']:<7} {job['status']:<11} {job['steps']:>5} "
                     f"{job['input_tokens'] + job['cache_write_tokens'] + job['cache_read_tokens']:>9,} "
                     f"{job['output_tokens']:>8,} {'-' if cost is None else f'${cost:.3f}':>8}")
        if cost is not None:
            by_band[band(job["rows"])].append(cost)

    priced = [c for costs in by_band.values() for c in costs]
    lines.append("")
    lines.append(f"{len(jobs)} runs, {len(priced)} with a known price, ${sum(priced):.2f} in total.")
    for _, name in BANDS + [(None, "over 30k rows")]:
        costs = by_band.get(name)
        if costs:
            lines.append(f"  {name}: {len(costs)} runs, average ${sum(costs) / len(costs):.3f}, "
                         f"highest ${max(costs):.3f}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--days", type=float, default=30, help="how far back to look (default 30)")
    print(report(Store(), parser.parse_args().days))
