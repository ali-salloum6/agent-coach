#!/usr/bin/env python3
"""Convert an existing Telegram JSONL export into readable Markdown.

This script does NOT call Telegram. It reads an already-exported
`exports/telegram_chat/messages.jsonl` (produced by
`scripts/export_telegram_chat.py`) and writes:

- A single server-seed file (default: `exports/telegram_chat_md/<agent>_seed.md`)
  that becomes the human-readable starting point for the new memory
  architecture on the server.
- One Markdown file per paired user/assistant exchange under
  `exports/telegram_chat_md/exchanges/`.
- An index file (`exports/telegram_chat_md/index.md`) listing every
  exchange with date, message IDs, and a short preview.

Goal: give us a clean audit surface tied to exact Telegram message IDs so
future memory-writer operations can be traced back to the source. The
existing `exports/` directory is already gitignored.

Usage:
  python scripts/telegram_jsonl_to_md.py
  python scripts/telegram_jsonl_to_md.py --jsonl exports/telegram_chat/messages.jsonl
  python scripts/telegram_jsonl_to_md.py --output-dir exports/telegram_chat_md --agent gym_coach
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSONL = ROOT / "exports" / "telegram_chat" / "messages.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "exports" / "telegram_chat_md"
MOSCOW = ZoneInfo("Europe/Moscow")

# Bot reply prefixes we can safely drop from the seed (they only mirror
# command UX and add no signal). User commands are dropped wholesale.
BOT_COMMAND_REPLY_PREFIXES = (
    "Hey! I'm your",
    "Fresh conversation started.",
    "Memory wiped.",
    "Nothing to forget",
    "Summarizing memory",
    "Memory summarized.",
    "Summarization failed.",
    "Model switched to",
    "Switched to cheap model",
    "OpenRouter key usage",
    "Checking usage for your current OpenRouter API key",
    "Current model:",
    "Web search is",
    "Web search ",
    "Noted: ",
    "Memory is empty",
)


@dataclass(frozen=True)
class Exchange:
    """A paired user/assistant turn reconstructed from the JSONL export."""

    index: int
    user_id: int
    assistant_id: int
    user_date_utc: str
    assistant_date_utc: str
    user_text: str
    assistant_text: str


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(
                    f"Invalid JSON on line {line_no} of {path}: {e}"
                ) from e
    return rows


def _is_command(text: str) -> bool:
    return text.strip().startswith("/")


def _is_bot_command_reply(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(p) for p in BOT_COMMAND_REPLY_PREFIXES)


def _clean_user_quote_prefix(text: str) -> str:
    # When the user "replies" to their previous message via Telegram quote,
    # we see "> Ali:\n<original>". Strip that prefix so the seed reads cleanly.
    return re.sub(r"^>\s*Ali:\s*\n", "", text.strip())


def _format_moscow(date_utc: str) -> str:
    if not date_utc:
        return ""
    try:
        dt = datetime.fromisoformat(date_utc).astimezone(MOSCOW)
    except ValueError:
        return date_utc
    return dt.strftime("%Y-%m-%d %H:%M Moscow")


def _date_only(date_utc: str) -> str:
    if not date_utc:
        return ""
    try:
        dt = datetime.fromisoformat(date_utc).astimezone(MOSCOW)
    except ValueError:
        return date_utc[:10]
    return dt.strftime("%Y-%m-%d")


def _preview(text: str, max_chars: int = 140) -> str:
    flat = re.sub(r"\s+", " ", text.strip())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1] + "…"


def _collect_exchanges(messages: Iterable[dict]) -> list[Exchange]:
    """Pair every user turn with the next bot reply that follows it."""
    exchanges: list[Exchange] = []
    pending_user: dict | None = None
    for msg in messages:
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if msg.get("out") is True:
            if _is_command(text):
                pending_user = None
                continue
            pending_user = msg
            continue
        if msg.get("out") is False:
            if pending_user is None:
                continue
            if _is_bot_command_reply(text):
                # Skip bot's automated command replies; keep waiting for the next real reply.
                continue
            user_text = _clean_user_quote_prefix(pending_user.get("text") or "")
            if user_text and text:
                exchanges.append(
                    Exchange(
                        index=len(exchanges) + 1,
                        user_id=int(pending_user["id"]),
                        assistant_id=int(msg["id"]),
                        user_date_utc=str(pending_user.get("date_utc") or ""),
                        assistant_date_utc=str(msg.get("date_utc") or ""),
                        user_text=user_text,
                        assistant_text=text,
                    )
                )
            pending_user = None
    return exchanges


def _exchange_markdown(exchange: Exchange, *, heading_level: int = 1) -> str:
    user_ts = _format_moscow(exchange.user_date_utc)
    asst_ts = _format_moscow(exchange.assistant_date_utc)
    h_main = "#" * heading_level
    h_sub = "#" * (heading_level + 1)
    parts = [
        f"{h_main} Exchange {exchange.index} — {user_ts}",
        "",
        f"- user_message_id: `{exchange.user_id}`",
        f"- assistant_message_id: `{exchange.assistant_id}`",
        f"- user_sent: `{user_ts}`",
        f"- assistant_sent: `{asst_ts}`",
        "",
        f"{h_sub} User",
        "",
        exchange.user_text,
        "",
        f"{h_sub} Assistant",
        "",
        exchange.assistant_text,
        "",
    ]
    return "\n".join(parts)


def _seed_header(
    *,
    agent: str,
    jsonl_path: Path,
    message_count: int,
    exchange_count: int,
    span: tuple[str, str] | None,
) -> str:
    span_line = (
        f"- chat_span: `{span[0]}` → `{span[1]}`" if span and span[0] and span[1] else ""
    )
    lines = [
        f"# {agent.replace('_', ' ').title()} — Telegram Chat Seed",
        "",
        "This file is the readable seed for the new long-term memory architecture.",
        "It was produced offline from an already-exported Telegram JSONL — no live",
        "Telegram API calls were made to create it. After the memory architecture",
        f"update lands, this seed will be promoted into `data/{agent}.md` (curated)",
        f"and `data/{agent}.recent.md` (short-lived) via the structured memory writer.",
        "",
        "## Provenance",
        "",
        f"- source_jsonl: `{jsonl_path}`",
        f"- generated_at_utc: `{datetime.now(timezone.utc).isoformat()}`",
        f"- raw_message_count: `{message_count}`",
        f"- paired_exchange_count: `{exchange_count}`",
    ]
    if span_line:
        lines.append(span_line)
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def _seed_body(exchanges: list[Exchange]) -> str:
    chunks: list[str] = []
    current_day = ""
    for exchange in exchanges:
        day = _date_only(exchange.user_date_utc)
        if day and day != current_day:
            chunks.append(f"## {day}\n")
            current_day = day
        chunks.append(_exchange_markdown(exchange, heading_level=3))
    return "\n".join(chunks)


def _index_markdown(exchanges: list[Exchange], *, agent: str) -> str:
    lines = [
        f"# {agent.replace('_', ' ').title()} — Exchange Index",
        "",
        f"- exchange_count: `{len(exchanges)}`",
        "",
        "| # | Date (Moscow) | User msg id | Assistant msg id | Preview |",
        "|---|---|---|---|---|",
    ]
    for exchange in exchanges:
        ts = _format_moscow(exchange.user_date_utc)
        preview = _preview(exchange.user_text)
        # Escape pipe characters so the table renders correctly.
        preview = preview.replace("|", "\\|")
        lines.append(
            f"| {exchange.index} | {ts} | "
            f"`{exchange.user_id}` | `{exchange.assistant_id}` | {preview} |"
        )
    lines.append("")
    return "\n".join(lines)


def convert(
    jsonl_path: Path,
    output_dir: Path,
    *,
    agent: str,
    write_per_exchange: bool,
) -> dict:
    if not jsonl_path.exists():
        raise SystemExit(
            f"JSONL export not found at {jsonl_path}. "
            "Run scripts/export_telegram_chat.py first."
        )

    messages = _read_jsonl(jsonl_path)
    exchanges = _collect_exchanges(messages)
    if not exchanges:
        raise SystemExit(f"No paired user/assistant exchanges found in {jsonl_path}.")

    output_dir.mkdir(parents=True, exist_ok=True)
    seed_path = output_dir / f"{agent}_seed.md"
    index_path = output_dir / "index.md"

    span = (
        _date_only(exchanges[0].user_date_utc),
        _date_only(exchanges[-1].assistant_date_utc or exchanges[-1].user_date_utc),
    )
    header = _seed_header(
        agent=agent,
        jsonl_path=jsonl_path,
        message_count=len(messages),
        exchange_count=len(exchanges),
        span=span,
    )
    body = _seed_body(exchanges)
    seed_path.write_text(header + body, encoding="utf-8")
    index_path.write_text(_index_markdown(exchanges, agent=agent), encoding="utf-8")

    per_exchange_dir: Path | None = None
    if write_per_exchange:
        per_exchange_dir = output_dir / "exchanges"
        per_exchange_dir.mkdir(parents=True, exist_ok=True)
        for exchange in exchanges:
            path = per_exchange_dir / f"exchange_{exchange.index:04d}.md"
            path.write_text(_exchange_markdown(exchange), encoding="utf-8")

    return {
        "seed_path": seed_path,
        "index_path": index_path,
        "exchanges_dir": per_exchange_dir,
        "message_count": len(messages),
        "exchange_count": len(exchanges),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convert an existing Telegram JSONL export into readable Markdown "
            "to seed the new memory architecture."
        )
    )
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--agent",
        default="gym_coach",
        help="Agent slug used in the seed filename (default: gym_coach).",
    )
    parser.add_argument(
        "--no-per-exchange",
        action="store_true",
        help="Skip writing one Markdown file per exchange.",
    )
    args = parser.parse_args()

    jsonl = args.jsonl if args.jsonl.is_absolute() else (ROOT / args.jsonl).resolve()
    out = (
        args.output_dir
        if args.output_dir.is_absolute()
        else (ROOT / args.output_dir).resolve()
    )

    try:
        result = convert(
            jsonl,
            out,
            agent=args.agent.strip() or "gym_coach",
            write_per_exchange=not args.no_per_exchange,
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130) from None

    print(f"seed:     {result['seed_path']}")
    print(f"index:    {result['index_path']}")
    if result["exchanges_dir"]:
        print(f"per-exch: {result['exchanges_dir']}")
    print(
        f"messages: {result['message_count']}  exchanges: {result['exchange_count']}"
    )


if __name__ == "__main__":
    main()
