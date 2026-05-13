#!/usr/bin/env python3
"""Dry-run the proposed structured memory writer against Telegram export samples.

This script is intentionally read-only with respect to bot memory. It loads
OPENROUTER_API_KEY_TEST from .env, samples user/bot exchanges from the Telegram
export, asks Gemini 3.1 Pro which JSON memory operations it would propose, and
writes prompts/responses to exports/memory_writer_validation/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT = ROOT / "exports" / "telegram_chat" / "messages.jsonl"
DEFAULT_OUTPUT_ROOT = ROOT / "exports" / "memory_writer_validation"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-3.1-pro-preview"


@dataclass(frozen=True)
class Exchange:
    index: int
    user_id: int
    assistant_id: int
    date_utc: str
    user_text: str
    assistant_text: str


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _is_command(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("/")


def _clean_user_quote_prefix(text: str) -> str:
    # Telegram sometimes preserves quoted forwarded/replied text as "> Ali:\n..."
    return re.sub(r"^>\s*Ali:\s*\n", "", text.strip())


def _collect_exchanges(messages: list[dict[str, Any]]) -> list[Exchange]:
    exchanges: list[Exchange] = []
    pending_user: dict[str, Any] | None = None
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

        if msg.get("out") is False and pending_user is not None:
            user_text = _clean_user_quote_prefix(pending_user.get("text") or "")
            assistant_text = text
            if user_text and assistant_text:
                exchanges.append(
                    Exchange(
                        index=len(exchanges) + 1,
                        user_id=int(pending_user["id"]),
                        assistant_id=int(msg["id"]),
                        date_utc=str(pending_user.get("date_utc") or msg.get("date_utc") or ""),
                        user_text=user_text,
                        assistant_text=assistant_text,
                    )
                )
            pending_user = None
    return exchanges


def _score_exchange(exchange: Exchange) -> int:
    text = f"{exchange.user_text}\n{exchange.assistant_text}".lower()
    keywords = [
        "weight",
        "kg",
        "sleep",
        "slept",
        "calorie",
        "protein",
        "back",
        "pain",
        "injury",
        "workout",
        "sets",
        "reps",
        "plan",
        "today",
        "ate",
        "restaurant",
        "skin",
        "creatine",
    ]
    score = sum(1 for word in keywords if word in text)
    score += min(len(exchange.user_text) // 300, 3)
    return score


def _select_samples(exchanges: list[Exchange], sample_count: int) -> list[Exchange]:
    if sample_count >= len(exchanges):
        return exchanges

    # Half evenly distributed over time, half high-signal by simple keyword score.
    even_count = max(1, sample_count // 2)
    selected: dict[int, Exchange] = {}
    if even_count == 1:
        selected[0] = exchanges[0]
    else:
        last = len(exchanges) - 1
        for i in range(even_count):
            idx = round(i * last / (even_count - 1))
            selected[idx] = exchanges[idx]

    ranked = sorted(
        enumerate(exchanges),
        key=lambda item: (_score_exchange(item[1]), len(item[1].user_text)),
        reverse=True,
    )
    for idx, exchange in ranked:
        selected[idx] = exchange
        if len(selected) >= sample_count:
            break

    return [selected[idx] for idx in sorted(selected)]


def _build_prompt(exchange: Exchange) -> str:
    return f"""You are testing a production memory writer for a long-term Telegram gym coach.

Your job: propose STRICT JSON memory operations for exactly one user/assistant exchange.

Return JSON only, with this top-level shape:
{{
  "ops": [
    {{
      "op": "noop",
      "reason": "nothing worth saving"
    }}
  ]
}}

Allowed operations:
- noop: nothing worth saving.
- add_recent_note: ephemeral detail useful for today/this week only.
- add_measurement: dated measurement/observation such as weight, sleep, calories, protein, pain level, waist, workout result.
- add_current_fact: explicit current state, goal, constraint, injury, routine, supplement, or durable preference stated by the user.
- add_event: meaningful dated event or milestone.
- add_plan: concrete assistant recommendation/protocol/plan. This may come from assistant advice.
- mark_adopted: user explicitly says they followed/adopted a previous plan.
- supersede: user explicitly corrects or overrides prior current state or plan.

Required fields for every non-noop op:
- op
- source: "user" or "assistant"
- date: YYYY-MM-DD if known, otherwise null
- summary: concise human-readable memory item
- evidence_quote: exact substring from the source text
- retention: "recent", "long_term", or "historical"

Extra fields when useful:
- category: e.g. weight, sleep, nutrition, workout, injury, pain, supplement, skin, logistics
- value
- unit
- ttl_days: required when retention is "recent"; use 2, 7, or 30
- status: proposed, active, adopted, superseded

Retention policy:
- Exact meal items, restaurant/menu browsing, transient locations, and "what happened today" logistics are recent unless they reveal a durable preference/pattern.
- Daily totals, sleep, weight, pain, body measurements, and workouts are long_term as compact measurements.
- Assistant advice can be saved as add_plan only if it is concrete and likely useful later for rationale.
- Assistant plans for "today", "this evening", "right now", or a single current flare-up are recent by default, usually ttl_days=2 or ttl_days=7.
- Assistant plans are long_term only when they are durable protocols/rules likely to recur, e.g. crisis protocol, injury safety rule, recurring supplement protocol, general exercise substitution rule, or user explicitly asks to keep using the plan.
- Do not save generic assistant explanations or pep talk.
- Do not save every workout prescription as long_term. Save the user's completed workout as a measurement; save the assistant's one-off workout plan as recent.
- Do not infer facts that are not explicitly stated.
- Do not convert assistant advice into user behavior.
- source="user" requires evidence_quote to be an exact substring of the USER MESSAGE.
- source="assistant" requires evidence_quote to be an exact substring of the ASSISTANT REPLY.
- If uncertain, choose noop or recent instead of long_term.
- Prefer at most 5 operations.

Known memory context for this dry run:
(Not provided. Judge only this exchange and avoid supersede unless the user explicitly says they are correcting an older fact.)

Exchange metadata:
- exchange_index: {exchange.index}
- user_message_id: {exchange.user_id}
- assistant_message_id: {exchange.assistant_id}
- date_utc: {exchange.date_utc}

USER MESSAGE:
{exchange.user_text}

ASSISTANT REPLY:
{exchange.assistant_text}
"""


async def _call_openrouter(api_key: str, prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning": {"effort": "medium"},
    }
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(OPENROUTER_URL, headers=headers, json=body)
        if resp.is_error:
            raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:1000]}")
        data = resp.json()
    return data["choices"][0]["message"]["content"]


def _safe_response_json(raw: str) -> Any:
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return json.loads(stripped)


def _write_exchange_file(path: Path, exchange: Exchange) -> None:
    path.write_text(
        "\n".join(
            [
                f"# Exchange {exchange.index}",
                "",
                f"- date_utc: `{exchange.date_utc}`",
                f"- user_message_id: `{exchange.user_id}`",
                f"- assistant_message_id: `{exchange.assistant_id}`",
                "",
                "## User",
                "",
                exchange.user_text,
                "",
                "## Assistant",
                "",
                exchange.assistant_text,
                "",
            ]
        ),
        encoding="utf-8",
    )


async def run(args: argparse.Namespace) -> Path:
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("OPENROUTER_API_KEY_TEST", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY_TEST is not set in .env")

    export_path = args.export
    if not export_path.is_absolute():
        export_path = (ROOT / export_path).resolve()
    output_root = args.output_root
    if not output_root.is_absolute():
        output_root = (ROOT / output_root).resolve()

    messages = _read_jsonl(export_path)
    exchanges = _collect_exchanges(messages)
    samples = _select_samples(exchanges, args.samples)

    run_dir = output_root / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, Any]] = []
    for i, exchange in enumerate(samples, start=1):
        prefix = f"sample_{i:02d}_exchange_{exchange.index:04d}"
        prompt = _build_prompt(exchange)

        _write_exchange_file(run_dir / f"{prefix}_exchange.md", exchange)
        (run_dir / f"{prefix}_prompt.md").write_text(prompt, encoding="utf-8")

        raw_response = await _call_openrouter(api_key, prompt)
        (run_dir / f"{prefix}_response_raw.txt").write_text(raw_response, encoding="utf-8")

        parsed: Any | None = None
        parse_error: str | None = None
        try:
            parsed = _safe_response_json(raw_response)
            (run_dir / f"{prefix}_response.json").write_text(
                json.dumps(parsed, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001 - this is a diagnostic script.
            parse_error = str(e)

        ops = parsed.get("ops", []) if isinstance(parsed, dict) else []
        summary_rows.append(
            {
                "sample": i,
                "exchange_index": exchange.index,
                "date_utc": exchange.date_utc,
                "user_message_id": exchange.user_id,
                "assistant_message_id": exchange.assistant_id,
                "operation_count": len(ops) if isinstance(ops, list) else None,
                "operations": [
                    {
                        "op": op.get("op"),
                        "source": op.get("source"),
                        "retention": op.get("retention"),
                        "category": op.get("category"),
                        "summary": op.get("summary") or op.get("reason"),
                    }
                    for op in ops
                    if isinstance(op, dict)
                ]
                if isinstance(ops, list)
                else [],
                "parse_error": parse_error,
            }
        )

    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "model": MODEL,
                "export_path": str(export_path),
                "message_count": len(messages),
                "exchange_count": len(exchanges),
                "sample_count": len(samples),
                "samples": summary_rows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "summary.md").write_text(_summary_markdown(summary_rows), encoding="utf-8")
    return run_dir


def _summary_markdown(rows: list[dict[str, Any]]) -> str:
    lines = ["# Memory Writer Validation Summary", ""]
    for row in rows:
        lines.extend(
            [
                f"## Sample {row['sample']} - Exchange {row['exchange_index']}",
                "",
                f"- date_utc: `{row['date_utc']}`",
                f"- user_message_id: `{row['user_message_id']}`",
                f"- assistant_message_id: `{row['assistant_message_id']}`",
                f"- operation_count: `{row['operation_count']}`",
            ]
        )
        if row.get("parse_error"):
            lines.append(f"- parse_error: `{row['parse_error']}`")
        lines.append("")
        for op in row["operations"]:
            lines.append(
                "- "
                f"`{op.get('op')}`"
                f" source=`{op.get('source')}`"
                f" retention=`{op.get('retention')}`"
                f" category=`{op.get('category')}`"
                f": {op.get('summary')}"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run structured memory JSON ops on Telegram export samples."
    )
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--samples", type=int, default=12)
    args = parser.parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be >= 1")
    run_dir = asyncio.run(run(args))
    print(run_dir)


if __name__ == "__main__":
    main()
