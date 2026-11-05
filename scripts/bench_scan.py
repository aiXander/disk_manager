"""Benchmark a full scan into a throw-away database.

Usage::

    uv run python scripts/bench_scan.py <dir> [<dir> ...] [--max-depth N]
        [--no-adaptive] [--no-skip] [--keep-db PATH]

Read-only on the scanned tree: the only thing written is a temporary SQLite
file (deleted afterwards unless ``--keep-db`` is given).  Prints wall time,
files/s, peak RSS, row count and the DB size.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import resource
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# BENCH_IMPL_DIR lets you benchmark another checkout of backend/ (e.g. the old code).
sys.path.insert(
    0, os.environ.get("BENCH_IMPL_DIR") or str(Path(__file__).resolve().parent.parent)
)

from backend.scanner import scan_roots  # noqa: E402

DEFAULT_SKIP = [
    "node_modules", ".git", "__pycache__", ".venv", "venv", "target", ".stack-work",
]


def _peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


async def _run(args: argparse.Namespace, db_path: Path) -> dict:
    roots = [Path(r).expanduser() for r in args.roots]
    t0 = time.perf_counter()
    events = 0
    last = None
    first_final_at = None
    walk_rss = None
    async for ev in scan_roots(
        scan_id="bench",
        roots=roots,
        max_depth=args.max_depth,
        adaptive=not args.no_adaptive,
        adaptive_threshold=args.adaptive_threshold,
        db_path=db_path,
        skip_dir_names=[] if args.no_skip else DEFAULT_SKIP,
    ):
        events += 1
        if getattr(ev, "phase", None) == "finalizing" and first_final_at is None:
            first_final_at = time.perf_counter() - t0
            walk_rss = _peak_rss_mb()
        last = ev
    wall = time.perf_counter() - t0
    return {"wall": wall, "events": events, "last": last, "walk_s": first_final_at,
            "walk_rss": walk_rss}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--max-depth", type=int, default=10)
    ap.add_argument("--adaptive-threshold", type=int, default=10000)
    ap.add_argument("--no-adaptive", action="store_true")
    ap.add_argument("--no-skip", action="store_true")
    ap.add_argument("--keep-db", type=Path, default=None)
    args = ap.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="bench_scan_"))
    db_path = args.keep_db or (tmpdir / "bench.db")
    try:
        res = asyncio.run(_run(args, db_path))
        last = res["last"]
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        root_size = conn.execute(
            "SELECT COALESCE(SUM(size), 0) FROM nodes WHERE depth = 0"
        ).fetchone()[0]
        conn.close()
        db_mb = sum(
            p.stat().st_size for p in db_path.parent.glob(db_path.name + "*")
        ) / 1e6
        files = last.files_scanned if last else 0
        print(f"status        {last.status if last else '?'}"
              f"{'  error=' + str(last.error) if last and last.error else ''}")
        print(f"wall          {res['wall']:.2f} s"
              + (f"  (walk {res['walk_s']:.2f} s)" if res["walk_s"] else ""))
        print(f"files         {files:,}  dirs {last.dirs_scanned if last else 0:,}"
              f"  errors {getattr(last, 'errors', '-')}")
        print(f"files/s       {files / res['wall']:,.0f}")
        print(f"rows in db    {rows:,}  ({db_mb:.1f} MB)")
        print(f"root size     {root_size / 1e9:.2f} GB (sum of depth-0 rows)")
        print(f"events        {res['events']}")
        print(f"peak RSS      {_peak_rss_mb():.0f} MB"
              + (f"  (during walk {res['walk_rss']:.0f} MB)" if res["walk_rss"] else ""))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
