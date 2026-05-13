#!/usr/bin/env python3
"""
Export a Telegram private chat with a bot using MTProto (Telethon).

Requires in .env (loaded automatically):
  TELEGRAM_API_ID
  TELEGRAM_API_HASH

Optional:
  TELEGRAM_EXPORT_BOT_USERNAME — default: coach_006_bot (without @)

First run opens an interactive login (phone + code). Session is saved under
exports/telegram_chat/ so later runs reuse it.

Usage:
  python scripts/export_telegram_chat.py
  python scripts/export_telegram_chat.py --output-dir exports/my_export
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of scripts/
ROOT = Path(__file__).resolve().parent.parent


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _msg_record(msg) -> dict:
    """Minimal stable JSON for each message (text chats + media awareness)."""
    text = getattr(msg, "message", None) or getattr(msg, "raw_text", None) or ""
    media = getattr(msg, "media", None)
    media_kind = type(media).__name__ if media else None
    rec: dict = {
        "id": msg.id,
        "date_utc": _utc_iso(msg.date),
        "out": bool(getattr(msg, "out", False)),
        "text": text,
        "media_type": media_kind,
    }
    r = getattr(msg, "reply_to", None)
    if r is not None:
        rid = getattr(r, "reply_to_msg_id", None)
        if rid is not None:
            rec["reply_to_msg_id"] = rid
    return rec


async def run_export(output_dir: Path, bot_username: str) -> Path:
    try:
        from telethon import TelegramClient
    except ImportError as e:
        raise SystemExit(
            "Missing dependency: install with `pip install telethon` "
            f"(import error: {e})"
        ) from e

    load_dotenv(ROOT / ".env")
    api_id_raw = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        raise SystemExit(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env (from my.telegram.org)."
        )
    try:
        api_id = int(api_id_raw)
    except ValueError as e:
        raise SystemExit("TELEGRAM_API_ID must be an integer.") from e

    output_dir.mkdir(parents=True, exist_ok=True)
    session_path = output_dir / "telethon_session"
    jsonl_path = output_dir / "messages.jsonl"
    meta_path = output_dir / "export_meta.json"

    client = TelegramClient(str(session_path), api_id, api_hash)

    async with client:
        await client.start()
        me = await client.get_me()
        entity = await client.get_entity(bot_username)
        peer_title = getattr(entity, "username", None) or str(entity.id)

        count = 0
        with jsonl_path.open("w", encoding="utf-8") as f:
            # Oldest first (easier for downstream replay / timeline rebuild).
            async for msg in client.iter_messages(entity, reverse=True):
                line = json.dumps(_msg_record(msg), ensure_ascii=False)
                f.write(line + "\n")
                count += 1

        meta = {
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
            "bot_username": bot_username,
            "peer": peer_title,
            "my_user_id": me.id if me else None,
            "message_count": count,
            "jsonl": jsonl_path.name,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Telegram chat with bot (MTProto).")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "exports" / "telegram_chat",
        help="Directory for session, messages.jsonl, and meta (default: exports/telegram_chat)",
    )
    parser.add_argument(
        "--bot",
        default=os.environ.get("TELEGRAM_EXPORT_BOT_USERNAME", "coach_006_bot").strip().lstrip("@"),
        help="Bot username without @ (default: coach_006_bot or TELEGRAM_EXPORT_BOT_USERNAME)",
    )
    args = parser.parse_args()
    out = args.output_dir
    if not out.is_absolute():
        out = (ROOT / out).resolve()

    try:
        path = asyncio.run(run_export(out, args.bot))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130) from None

    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
