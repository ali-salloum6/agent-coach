#!/usr/bin/env python3
"""Build a curated `data/<agent>.md` (and `<agent>.recent.md`) from the Telegram export.

This is the production "first fill" of the new structured memory. It does
NOT call the readable-seed converter. It calls the SAME pipeline that will
run live in the bot — `memory_writer.propose_and_apply()` — once per
historical exchange, in chronological order. Each call:

- asks the configured writer model for strict JSON memory operations,
- deterministic code validates them,
- accepted ops are filed under the right Markdown section in
  `data/<agent>.md`, or written to `data/<agent>.recent.md` with a TTL.

Because each call also prunes already-expired recent notes, by the time
the last exchange is processed only the most recent few days of recent
notes survive. The result is a curated long-term memory file plus a tiny
recent-notes file — both small enough to load into the prompt.

Usage (smoke test on 20 exchanges first):

  python3 scripts/seed_memory_from_export.py --limit 20

Full run, using the dedicated test API key so prod isn't billed:

  python3 scripts/seed_memory_from_export.py

If something looks off, the old memory file is preserved as
`data/<agent>.backup_<timestamp>.md` so you can roll back by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Force line-buffered stdout/stderr so progress is visible in piped/log shells.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
print = functools.partial(print, flush=True)  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

# IMPORTANT: swap the API key BEFORE importing config so we never touch
# the production key during seeding by accident.
_test_key = (os.environ.get("OPENROUTER_API_KEY_TEST") or "").strip()
_prod_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
_active_key = _test_key or _prod_key
if not _active_key:
    raise SystemExit(
        "Set OPENROUTER_API_KEY_TEST (preferred) or OPENROUTER_API_KEY in .env."
    )
os.environ["OPENROUTER_API_KEY"] = _active_key

import config  # noqa: E402
import memory_writer  # noqa: E402

# We reuse the well-tested exchange reconstruction from the validation
# script so this seeder pairs user/assistant turns identically.
from validate_memory_writer_ops import (  # type: ignore  # noqa: E402
    Exchange,
    _collect_exchanges,
    _read_jsonl,
)

MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_JSONL = ROOT / "exports" / "telegram_chat" / "messages.jsonl"


def _exchange_timestamp(exchange: Exchange) -> datetime:
    raw = exchange.date_utc
    if not raw:
        return datetime.now(MOSCOW)
    try:
        return datetime.fromisoformat(raw).astimezone(MOSCOW)
    except ValueError:
        return datetime.now(MOSCOW)


def _backup_existing(agent_slug: str) -> list[Path]:
    backed: list[Path] = []
    timestamp = datetime.now(MOSCOW).strftime("%Y%m%d_%H%M%S")
    for suffix in (".md", ".recent.md", ".memory_ops.jsonl"):
        path = config.DATA_DIR / f"{agent_slug}{suffix}"
        if not path.exists():
            continue
        dest = path.with_name(path.stem + f".backup_pre_seed_{timestamp}" + path.suffix)
        shutil.move(path, dest)
        backed.append(dest)
    return backed


async def _process_one(
    exchange: Exchange,
    *,
    agent_slug: str,
    writer_model: str,
    max_retries: int = 3,
) -> memory_writer.ApplyResult:
    """Call the writer with bounded retries on transient network errors.

    The bot has its own error handling but the seeder hits 941 calls in a
    row, so a flaky network or a slow Gemini call (read timeout, brief
    connection reset) shouldn't end the whole batch. Retry on httpx
    transport errors specifically; let the underlying `propose_and_apply`
    handle JSON parse / validation issues.
    """
    import httpx  # local import keeps top-level imports tidy

    delay = 4.0
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return await memory_writer.propose_and_apply(
                agent_slug,
                exchange.user_text,
                exchange.assistant_text,
                timestamp=_exchange_timestamp(exchange),
                writer_model=writer_model,
            )
        except (
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
            httpx.PoolTimeout,
        ) as e:
            last_exc = e
            print(
                f"  [retry {attempt}/{max_retries}] exchange#{exchange.index} "
                f"transport error: {type(e).__name__}: {e}"
            )
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc


async def run(
    *,
    jsonl_path: Path,
    agent_slug: str,
    limit: int | None,
    start: int,
    writer_model: str,
    backup: bool,
    sleep_seconds: float,
) -> None:
    if not jsonl_path.exists():
        raise SystemExit(f"JSONL export not found: {jsonl_path}")

    messages = _read_jsonl(jsonl_path)
    exchanges = _collect_exchanges(messages)
    end = len(exchanges) if limit is None else min(len(exchanges), start + limit)
    selected = exchanges[start:end]
    if not selected:
        raise SystemExit(
            f"No exchanges to process in range [{start}, {end}). "
            f"Found {len(exchanges)} total."
        )

    if backup:
        backed = _backup_existing(agent_slug)
        if backed:
            print("Backed up existing memory files:")
            for path in backed:
                print(f"  - {path}")
        else:
            print("No existing memory files to back up.")

    memory_writer.ensure_files(agent_slug)
    canonical_path = config.DATA_DIR / f"{agent_slug}.md"
    recent_path = config.DATA_DIR / f"{agent_slug}.recent.md"

    print(
        f"Seeding agent={agent_slug!r} using model={writer_model!r} "
        f"on {len(selected)} exchanges (of {len(exchanges)} total) "
        f"starting at index {start}…"
    )
    if _test_key:
        print("Using OPENROUTER_API_KEY_TEST for this run.")
    else:
        print("Using OPENROUTER_API_KEY (production). Beware billing.")

    totals = {"accepted": 0, "rejected": 0, "noop_calls": 0, "parse_errors": 0}
    started = time.time()
    for i, exchange in enumerate(selected, start=1):
        try:
            result = await _process_one(
                exchange, agent_slug=agent_slug, writer_model=writer_model
            )
        except Exception as e:  # noqa: BLE001 - we want to keep going.
            print(f"  [{i}/{len(selected)}] exchange#{exchange.index} ERROR: {e}")
            totals["parse_errors"] += 1
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
            continue

        totals["accepted"] += result.accepted
        totals["rejected"] += result.rejected
        if result.accepted == 0 and result.rejected == 0:
            totals["noop_calls"] += 1
        if result.parse_error:
            totals["parse_errors"] += 1

        if i % 10 == 0 or i == len(selected) or i <= 3:
            elapsed = time.time() - started
            rate = i / elapsed if elapsed > 0 else 0
            remaining = (len(selected) - i) / rate if rate > 0 else 0
            canonical_size = (
                canonical_path.stat().st_size if canonical_path.exists() else 0
            )
            recent_size = (
                recent_path.stat().st_size if recent_path.exists() else 0
            )
            print(
                f"  [{i}/{len(selected)}] exchange#{exchange.index} "
                f"acc={result.accepted} rej={result.rejected} "
                f"pruned={result.pruned_recent} "
                f"canonical={canonical_size:,}B recent={recent_size:,}B "
                f"({rate:.2f}/s, ~{remaining:.0f}s left)"
            )
        if sleep_seconds > 0 and i < len(selected):
            await asyncio.sleep(sleep_seconds)

    elapsed = time.time() - started
    canonical_size = canonical_path.stat().st_size if canonical_path.exists() else 0
    recent_size = recent_path.stat().st_size if recent_path.exists() else 0
    audit_path = config.DATA_DIR / f"{agent_slug}.memory_ops.jsonl"
    audit_size = audit_path.stat().st_size if audit_path.exists() else 0

    print()
    print("=== Seeding complete ===")
    print(f"  Exchanges processed: {len(selected)}")
    print(f"  Accepted ops:        {totals['accepted']}")
    print(f"  Rejected ops:        {totals['rejected']}")
    print(f"  Noop-only calls:     {totals['noop_calls']}")
    print(f"  Parse/model errors:  {totals['parse_errors']}")
    print(f"  Elapsed:             {elapsed:.1f}s")
    print()
    print(f"  Canonical memory:    {canonical_path}  ({canonical_size:,} bytes)")
    print(f"  Recent memory:       {recent_path}  ({recent_size:,} bytes)")
    print(f"  Audit log:           {audit_path}  ({audit_size:,} bytes)")
    print()
    print(
        "Inspect data/gym_coach.md by hand. Edit/trim as needed before "
        "uploading to the server."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--agent", default="gym_coach")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many exchanges (good for sanity-checking).",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Start at this exchange index (0-based, inclusive).",
    )
    parser.add_argument(
        "--writer-model",
        default=config.DEFAULT_MODEL,
        help=f"Writer model (default: {config.DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do NOT back up existing data/<agent>.* files before seeding.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between calls (rate-limit guard).",
    )
    args = parser.parse_args()

    jsonl = args.jsonl if args.jsonl.is_absolute() else (ROOT / args.jsonl).resolve()
    asyncio.run(
        run(
            jsonl_path=jsonl,
            agent_slug=args.agent,
            limit=args.limit,
            start=args.start,
            writer_model=args.writer_model,
            backup=not args.no_backup,
            sleep_seconds=args.sleep,
        )
    )


if __name__ == "__main__":
    main()
