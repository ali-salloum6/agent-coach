from __future__ import annotations

import asyncio
import base64
import html
import itertools
import logging
import re
import time
from io import BytesIO
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from telegram import BotCommand, InputFile, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut

import config
import llm
import memory
import tts
from agents.registry import get_agent

MAX_TG_LEN = 4096
TELEGRAM_TRANSIENT_ERRORS = (TimedOut, NetworkError)


def _markdown_to_telegram_html(text: str) -> str:
    """Convert common Markdown to Telegram HTML so LLM replies format correctly."""
    text = html.escape(text)
    # Bold: **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # Line-start asterisk bullets → bullet character so they don't become italic
    text = re.sub(r"^\* +", "• ", text, flags=re.MULTILINE)
    # Italic: *text* (single asterisk)
    text = re.sub(r"\*([^*]+?)\*", r"<i>\1</i>", text, flags=re.DOTALL)
    # Italic: _text_
    text = re.sub(r"_([^_]+?)_", r"<i>\1</i>", text, flags=re.DOTALL)
    # Code: `text`
    text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text, flags=re.DOTALL)
    # Headers: ### or ## or # at line start
    text = re.sub(r"^### (.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    return text


def _truncate_for_telegram(text: str, max_len: int = 350) -> str:
    text = text.strip().replace("\r", " ").replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _format_openrouter_http_error_for_telegram(
    e: httpx.HTTPStatusError,
    *,
    model: str | None,
) -> str:
    status = e.response.status_code

    hint_by_status: dict[int, str] = {
        402: "OpenRouter billing error: insufficient credits (HTTP 402).",
        401: "OpenRouter authentication failed (HTTP 401). Check `OPENROUTER_API_KEY`.",
        403: "OpenRouter request forbidden (HTTP 403). Check whether this API key has access.",
        429: "OpenRouter rate limited your request (HTTP 429). Try again in a bit or use `/cheap`.",
    }
    hint = hint_by_status.get(status, f"OpenRouter returned HTTP {status} (request failed).")

    details = ""
    try:
        data = e.response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                details = err["message"]
            elif isinstance(data.get("message"), str):
                details = data["message"]
    except Exception:
        pass
    if not details:
        details = (e.response.text or "")

    model_label = html.escape(str(model)) if model else ""
    details_short = _truncate_for_telegram(details)

    extra = ""
    if status == 402:
        extra = "\nRun `/openrouter_balance` to check your remaining credits."

    return (
        f"{hint}{extra}\n"
        f"Model: <code>{model_label}</code>\n"
        f"Details: <code>{html.escape(details_short)}</code>"
    )


def _format_openrouter_runtime_error_for_telegram(
    exc: RuntimeError,
    *,
    model: str | None,
    after_stream_failure: bool = False,
) -> str:
    """User-facing explanation for llm.chat / chat_stream RuntimeError paths."""
    raw = str(exc).strip()
    stream_detail: str | None = None
    api_detail: str | None = None
    if raw.startswith("OpenRouter stream error: "):
        stream_detail = raw[len("OpenRouter stream error: ") :].strip()
    elif raw.startswith("OpenRouter error: "):
        api_detail = raw[len("OpenRouter error: ") :].strip()

    detail = (stream_detail or api_detail or raw).strip()
    detail_lower = detail.lower()
    raw_lower = raw.lower()

    if "missing 'choices'" in raw_lower or "unexpected format" in raw_lower:
        headline = (
            "The provider returned a response the bot could not parse (no normal "
            "<code>choices</code> field). Often this is an error payload in disguise."
        )
        hint = (
            "Try again in a moment, or switch model with <code>/model</code>. "
            "If it keeps happening, check <a href=\"https://openrouter.ai/status\">OpenRouter status</a>."
        )
    elif "internal server error" in detail_lower or any(
        x in detail_lower for x in ("502", "503", "504", "bad gateway", "gateway timeout")
    ):
        headline = (
            "The model provider (OpenRouter or the upstream host, e.g. Google) hit a "
            "<b>temporary server error</b>. Your request did not succeed on their side."
        )
        hint = (
            "Wait a minute and try again, use <code>/model</code> to pick another model, "
            "or <code>/cheap</code> for a lighter one. "
            "<a href=\"https://openrouter.ai/status\">OpenRouter status</a>"
        )
    elif "provider returned error" in detail_lower:
        headline = "The upstream provider reported an error for this request (not a bug in your message)."
        hint = (
            "Retry shortly or change model with <code>/model</code>. "
            "<a href=\"https://openrouter.ai/status\">OpenRouter status</a>"
        )
    elif "rate limit" in detail_lower or "429" in detail_lower or "too many requests" in detail_lower:
        headline = "The provider rate-limited this request."
        hint = "Wait a bit, try <code>/cheap</code>, or add your own API key on OpenRouter for higher limits."
    else:
        headline = "The model provider returned an error."
        hint = (
            "Try again, or switch model with <code>/model</code>. "
            "<a href=\"https://openrouter.ai/status\">OpenRouter status</a>"
        )

    parts = [headline, "", hint]
    if after_stream_failure:
        parts.extend(
            [
                "",
                "<i>Note:</i> streaming failed first; a non-streaming retry failed too.",
            ]
        )
    model_label = html.escape(str(model)) if model else "(unknown)"
    parts.extend(
        [
            "",
            f"Model: <code>{model_label}</code>",
            f"Provider message: <code>{html.escape(_truncate_for_telegram(detail))}</code>",
        ]
    )
    return "\n".join(parts)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

AGENT = get_agent(config.ACTIVE_AGENT)

session: dict = {
    "model": AGENT.default_model,
    "history": [],
    "web_search": False,
    "tts": config.TTS_AUTO_READ,
}

STREAM_FLUSH_INTERVAL_SECONDS = 0.25
STREAM_FLUSH_MIN_CHARS = 24
_draft_id_counter = itertools.count(start=int(time.time()))

_album_lock = asyncio.Lock()
_album_pending: dict[str, dict] = {}
_tts_lock = asyncio.Lock()


def _is_allowed_user(update: Update) -> bool:
    """True if no restriction is set, or the update is from the allowed user."""
    if not config.ALLOWED_USERNAME and config.ALLOWED_USER_ID is None:
        return True
    user = update.effective_user
    if not user:
        return False
    if config.ALLOWED_USERNAME and (user.username or "").lower() == config.ALLOWED_USERNAME.lower():
        return True
    if config.ALLOWED_USER_ID is not None and user.id == config.ALLOWED_USER_ID:
        return True
    return False


def _reset_session(model: str | None = None) -> None:
    # Prune expired recent notes BEFORE building the prompt so the model
    # never sees stale TTL entries on a fresh conversation.
    try:
        pruned = memory.prune_recent(AGENT.slug)
        if pruned:
            log.info("Pruned %d expired recent notes on /new", pruned)
    except Exception:
        log.exception("Pruning recent notes failed (non-fatal)")
    mem = memory.load(AGENT.slug)
    system_prompt = AGENT.build_system_prompt(mem)
    session["model"] = model or AGENT.default_model
    session["history"] = [{"role": "system", "content": system_prompt}]
    session["web_search"] = False
    session["tts"] = config.TTS_AUTO_READ


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    _reset_session()
    name = html.escape(AGENT.name)
    model = html.escape(session["model"])
    await update.message.reply_text(
        f"Hey! I'm your <b>{name}</b>.\n\n"
        "Commands:\n"
        "/new [model|cheap|mid|max] — start a fresh conversation (mid = gemini-3-flash; max = search on)\n"
        "/search on|off — toggle web search\n"
        "/remember &lt;text&gt; — save a note to memory\n"
        "/memory — show what I remember\n"
        "/model &lt;slug&gt; — switch LLM model\n"
        "/cheap — use 3.1 flash lite (cheaper)\n"
        "/openrouter_balance — check OpenRouter remaining credits\n"
        "/read on|off — auto-read replies aloud (on by default)\n\n"
        "Send <b>photos</b> or image files with optional captions; photo albums are "
        "grouped into one message for the model.\n\n"
        f"Current model: <code>{model}</code>",
        parse_mode="HTML",
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    raw = " ".join(ctx.args) if ctx.args else None
    raw_lower = raw.strip().lower() if raw else None
    if raw_lower == "cheap":
        model = config.CHEAP_MODEL
        search_on = False
    elif raw_lower == "mid":
        model = config.EXTRACTION_MODEL
        search_on = False
    elif raw_lower == "max":
        model = None
        search_on = True
    else:
        model = raw
        search_on = False
    _reset_session(model)
    if search_on:
        session["web_search"] = True
    model_esc = html.escape(session["model"])
    msg = f"Fresh conversation started.\nModel: <code>{model_esc}</code>"
    if session["web_search"]:
        msg += "\nWeb search <b>on</b>."
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    if not ctx.args:
        status = "on" if session["web_search"] else "off"
        await update.message.reply_text(f"Web search is <b>{status}</b>.", parse_mode="HTML")
        return
    flag = ctx.args[0].lower()
    if flag == "on":
        session["web_search"] = True
        await update.message.reply_text("Web search <b>enabled</b> for this conversation.", parse_mode="HTML")
    elif flag == "off":
        session["web_search"] = False
        await update.message.reply_text("Web search <b>disabled</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text("Usage: /search on|off")


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text("Usage: /remember <something to remember>")
        return
    memory.append(AGENT.slug, text)
    text_esc = html.escape(text)
    await update.message.reply_text(f"Noted: <i>{text_esc}</i>", parse_mode="HTML")


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    mem = memory.load(AGENT.slug)
    if not mem.strip():
        await update.message.reply_text("Memory is empty.")
        return
    # Telegram messages max out at 4096 chars — keep the end (most recent entries)
    if len(mem) > 4000:
        mem = "…(earlier memory omitted)\n\n" + mem[-4000:]
    mem_html = _markdown_to_telegram_html(mem)
    try:
        await update.message.reply_text(mem_html, parse_mode="HTML")
    except BadRequest:
        await update.message.reply_text(mem)


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    if not ctx.args:
        model_esc = html.escape(session["model"])
        await update.message.reply_text(
            f"Current model: <code>{model_esc}</code>",
            parse_mode="HTML",
        )
        return
    slug = ctx.args[0].strip().lower()
    if slug == "mid":
        session["model"] = config.EXTRACTION_MODEL
    elif slug == "cheap":
        session["model"] = config.CHEAP_MODEL
    elif slug == "max":
        session["model"] = config.DEFAULT_MODEL
    else:
        session["model"] = ctx.args[0]
    model_esc = html.escape(session["model"])
    await update.message.reply_text(
        f"Model switched to <code>{model_esc}</code>",
        parse_mode="HTML",
    )


async def cmd_cheap(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    session["model"] = config.CHEAP_MODEL
    model_esc = html.escape(config.CHEAP_MODEL)
    await update.message.reply_text(
        f"Switched to cheap model: <code>{model_esc}</code>",
        parse_mode="HTML",
    )


def _last_assistant_reply() -> str | None:
    for msg in reversed(session["history"]):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


async def _upload_tts_audio(message, wav_bytes: bytes) -> None:
    audio_file = InputFile(BytesIO(wav_bytes), filename="reply.wav")
    try:
        await message.reply_audio(audio_file, title="Reply")
    except TELEGRAM_TRANSIENT_ERRORS:
        log.warning("Telegram audio upload timed out; retrying once")
        await asyncio.sleep(1.0)
        audio_file = InputFile(BytesIO(wav_bytes), filename="reply.wav")
        await message.reply_audio(audio_file, title="Reply")


async def _send_tts_audio(
    message,
    text: str,
    *,
    status_message: bool = False,
) -> None:
    """Synthesize *text* and send WAV audio to the chat."""
    if not config.TTS_ENABLED:
        if status_message:
            await message.reply_text("Text-to-speech is disabled.")
        return

    if not tts.is_speakable(text):
        if status_message:
            await message.reply_text("Nothing speakable in the last reply.")
        return

    clean_len = len(tts.reply_to_speech_text(text))
    if clean_len > tts.MAX_TTS_CHARS:
        msg = (
            f"Last reply is too long to read aloud ({clean_len:,} chars; "
            f"max {tts.MAX_TTS_CHARS:,}). Ask for a shorter summary first."
        )
        if status_message:
            await message.reply_text(msg)
        else:
            log.warning("Auto-read skipped: %s", msg)
        return

    if _tts_lock.locked():
        if status_message:
            await message.reply_text("Already generating audio — please wait.")
        else:
            log.info("Auto-read skipped: TTS already in progress")
        return

    if status_message:
        await message.reply_text("Generating audio…")
    try:
        await message.chat.send_action("record_voice")
    except TelegramError:
        log.warning("send_chat_action record_voice failed; continuing")

    async with _tts_lock:
        try:
            wav_bytes = await asyncio.to_thread(tts.synthesize_to_wav_bytes, text)
        except ValueError as e:
            if status_message:
                await message.reply_text(str(e))
            else:
                log.warning("Auto-read synthesis skipped: %s", e)
            return
        except Exception:
            log.exception("TTS synthesis failed")
            if status_message:
                await message.reply_text("Could not generate audio. Try again later.")
            return

    try:
        await _upload_tts_audio(message, wav_bytes)
    except Exception:
        log.exception("Failed to send TTS audio")
        if status_message:
            await message.reply_text(
                "Audio was generated but failed to upload. Try again."
            )


async def _background_tts(update: Update, reply: str) -> None:
    message = update.effective_message
    if message is None:
        return
    try:
        await _send_tts_audio(message, reply, status_message=False)
    except Exception:
        log.exception("Background TTS failed (non-fatal)")


async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    if not config.TTS_ENABLED:
        await update.message.reply_text("Text-to-speech is disabled.")
        return

    if not ctx.args:
        status = "on" if session["tts"] else "off"
        await update.message.reply_text(
            f"Auto-read is <b>{status}</b>. Each reply includes audio when on.\n"
            "/read on|off — toggle · /read last — replay last reply",
            parse_mode="HTML",
        )
        return

    flag = ctx.args[0].lower()
    if flag == "on":
        session["tts"] = True
        await update.message.reply_text("Auto-read <b>enabled</b>.", parse_mode="HTML")
        return
    if flag == "off":
        session["tts"] = False
        await update.message.reply_text("Auto-read <b>disabled</b>.", parse_mode="HTML")
        return
    if flag != "last":
        await update.message.reply_text("Usage: /read on|off · /read last")
        return

    reply = _last_assistant_reply()
    if not reply:
        await update.message.reply_text("Nothing to read yet — send a message first.")
        return

    await _send_tts_audio(update.message, reply, status_message=True)


async def cmd_openrouter_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    await update.message.reply_text("Checking usage for your current OpenRouter API key…")

    try:
        info = await llm.get_openrouter_key_info()
        usage = info.get("usage")
        usage_daily = info.get("usage_daily")
        usage_weekly = info.get("usage_weekly")
        usage_monthly = info.get("usage_monthly")
        limit = info.get("limit")
        limit_reset = info.get("limit_reset")
        limit_remaining = info.get("limit_remaining")
        label = info.get("label")
        free_tier = info.get("is_free_tier")

        def _fmt_2(value: object) -> str:
            if isinstance(value, (int, float)):
                return f"{value:.2f}"
            return str(value)

        def _next_reset_utc(reset_cycle: object) -> str:
            if not isinstance(reset_cycle, str):
                return "unknown"
            now = datetime.now(ZoneInfo("UTC"))
            cycle = reset_cycle.strip().lower()
            if cycle == "daily":
                next_ts = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            elif cycle == "weekly":
                days_until_monday = (7 - now.weekday()) % 7
                if days_until_monday == 0:
                    days_until_monday = 7
                next_ts = (now + timedelta(days=days_until_monday)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            elif cycle == "monthly":
                if now.month == 12:
                    next_ts = now.replace(
                        year=now.year + 1,
                        month=1,
                        day=1,
                        hour=0,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                else:
                    next_ts = now.replace(
                        month=now.month + 1,
                        day=1,
                        hour=0,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
            else:
                return "unknown"
            return next_ts.strftime("%Y-%m-%d %H:%M UTC")

        await update.message.reply_text(
            "OpenRouter key usage (this specific API key):\n"
            f"Key label: <code>{html.escape(str(label))}</code>\n"
            f"Usage total: <code>{html.escape(_fmt_2(usage))}</code>\n"
            f"Usage today (UTC): <code>{html.escape(_fmt_2(usage_daily))}</code>\n"
            f"Usage this week (UTC): <code>{html.escape(_fmt_2(usage_weekly))}</code>\n"
            f"Usage this month (UTC): <code>{html.escape(_fmt_2(usage_monthly))}</code>\n"
            f"Key limit: <code>{html.escape(_fmt_2(limit))}</code>\n"
            f"Limit reset cycle: <code>{html.escape(str(limit_reset))}</code>\n"
            f"Next reset (UTC): <code>{html.escape(_next_reset_utc(limit_reset))}</code>\n"
            f"Key remaining: <code>{html.escape(_fmt_2(limit_remaining))}</code>\n"
            f"Free tier key: <code>{html.escape(str(free_tier))}</code>",
            parse_mode="HTML",
        )
    except httpx.HTTPStatusError as e:
        msg = _format_openrouter_http_error_for_telegram(e, model="key-info")
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception:
        log.exception("OpenRouter key usage check failed")
        await update.message.reply_text("Failed to check OpenRouter key usage. Try again later.")


async def _download_telegram_file(bot, file_id: str, *, max_bytes: int) -> bytes:
    tg_file = await bot.get_file(file_id)
    if tg_file.file_size is not None and tg_file.file_size > max_bytes:
        raise ValueError(
            f"File too large ({tg_file.file_size} bytes; max {max_bytes // (1024 * 1024)} MB)."
        )
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    data = buf.getvalue()
    if len(data) > max_bytes:
        raise ValueError(
            f"Downloaded file too large ({len(data)} bytes; max {max_bytes // (1024 * 1024)} MB)."
        )
    return data


def _openrouter_image_part(raw: bytes, mime_type: str) -> dict:
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{b64}"},
    }


def _vision_memory_summary(ts: str, captions: list[str | None]) -> str:
    lines = [
        f"[Sent at {ts} Moscow]",
        f"User sent {len(captions)} image(s).",
    ]
    for i, cap in enumerate(captions, start=1):
        c = (cap or "").strip()
        lines.append(f"Image {i} caption: {c if c else '(none)'}")
    return "\n".join(lines)


async def _conversation_reply(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user_content: str | list,
    memory_user_text: str,
    stream: bool,
    web_search: bool,
    allow_fallback: bool,
    model: str | None = None,
) -> None:
    """Append user turn, call the model, send reply, schedule memory extraction."""
    chat_model = model or session["model"]
    session["history"].append({"role": "user", "content": user_content})

    await _safe_send_action(update)

    reply_parts: list[str] = []
    chat_id = update.effective_chat.id if update.effective_chat else None
    can_stream_draft = (
        stream
        and chat_id is not None
        and update.effective_chat is not None
        and update.effective_chat.type == "private"
    )
    draft_id = next(_draft_id_counter)
    loop = asyncio.get_running_loop()
    last_flush_at = loop.time()
    pending_chars = 0
    response_meta: dict = {}
    reply: str | None = None

    if stream:
        try:
            async for delta in llm.chat_stream(
                messages=session["history"],
                model=chat_model,
                web_search=web_search,
                response_meta=response_meta,
                allow_fallback=allow_fallback,
            ):
                reply_parts.append(delta)
                pending_chars += len(delta)

                if not can_stream_draft:
                    continue

                now = loop.time()
                should_flush = (
                    pending_chars >= STREAM_FLUSH_MIN_CHARS
                    or (now - last_flush_at) >= STREAM_FLUSH_INTERVAL_SECONDS
                )
                if not should_flush:
                    continue

                partial_text = "".join(reply_parts)[:MAX_TG_LEN]
                if partial_text.strip():
                    can_stream_draft = await _send_draft(ctx, chat_id, draft_id, partial_text)
                pending_chars = 0
                last_flush_at = now

        except Exception as exc:
            if not reply_parts:
                log.exception("LLM stream failed, trying non-stream fallback")
                fallback_attempts = 2 if isinstance(exc, httpx.ReadError) else 1
                if fallback_attempts > 1:
                    await update.message.reply_text(
                        "Temporary network issue while contacting the model provider. Retrying once."
                    )
                try:
                    for attempt in range(fallback_attempts):
                        try:
                            reply = await llm.chat(
                                messages=session["history"],
                                model=chat_model,
                                web_search=web_search,
                                response_meta=response_meta,
                                allow_fallback=allow_fallback,
                            )
                            break
                        except httpx.ReadError:
                            if attempt + 1 < fallback_attempts:
                                log.warning(
                                    "LLM non-stream fallback hit ReadError; retrying once"
                                )
                                await asyncio.sleep(1.0)
                                continue
                            raise
                except httpx.HTTPStatusError as e:
                    msg = _format_openrouter_http_error_for_telegram(
                        e, model=session.get("model")
                    )
                    await update.message.reply_text(msg, parse_mode="HTML")
                    session["history"].pop()
                    return
                except Exception as fallback_exc:
                    log.exception("LLM call failed")
                    if isinstance(exc, httpx.HTTPStatusError):
                        msg = _format_openrouter_http_error_for_telegram(
                            exc, model=session.get("model")
                        )
                        await update.message.reply_text(msg, parse_mode="HTML")
                    elif isinstance(fallback_exc, httpx.ReadError):
                        await update.message.reply_text(
                            "Model provider connection dropped again after one retry. Please try again."
                        )
                    elif isinstance(fallback_exc, RuntimeError):
                        msg = _format_openrouter_runtime_error_for_telegram(
                            fallback_exc,
                            model=session.get("model"),
                            after_stream_failure=True,
                        )
                        await update.message.reply_text(msg, parse_mode="HTML")
                    else:
                        await update.message.reply_text(
                            "Something went wrong talking to the model. Try again."
                        )
                    session["history"].pop()
                    return
            else:
                log.exception("LLM stream failed mid-response, using collected partial reply")
                reply = "".join(reply_parts)
        else:
            if can_stream_draft and pending_chars > 0:
                partial_text = "".join(reply_parts)[:MAX_TG_LEN]
                if partial_text.strip():
                    await _send_draft(ctx, chat_id, draft_id, partial_text)
            reply = "".join(reply_parts)
    else:
        try:
            reply = await llm.chat(
                messages=session["history"],
                model=chat_model,
                web_search=web_search,
                response_meta=response_meta,
                allow_fallback=allow_fallback,
            )
        except httpx.HTTPStatusError as e:
            msg = _format_openrouter_http_error_for_telegram(e, model=session.get("model"))
            await update.message.reply_text(msg, parse_mode="HTML")
            session["history"].pop()
            return
        except httpx.ReadError:
            log.exception("LLM call failed (ReadError), retrying once")
            await update.message.reply_text(
                "Temporary network issue while contacting the model provider. Retrying once."
            )
            try:
                await asyncio.sleep(1.0)
                reply = await llm.chat(
                    messages=session["history"],
                    model=chat_model,
                    web_search=web_search,
                    response_meta=response_meta,
                    allow_fallback=allow_fallback,
                )
            except httpx.ReadError:
                log.exception("LLM call failed after retry")
                await update.message.reply_text(
                    "Model provider connection dropped again after one retry. Please try again."
                )
                session["history"].pop()
                return
            except httpx.HTTPStatusError as e:
                msg = _format_openrouter_http_error_for_telegram(
                    e, model=session.get("model")
                )
                await update.message.reply_text(msg, parse_mode="HTML")
                session["history"].pop()
                return
            except Exception as e:
                log.exception("LLM call failed after ReadError retry")
                if isinstance(e, RuntimeError):
                    msg = _format_openrouter_runtime_error_for_telegram(
                        e,
                        model=session.get("model"),
                    )
                    await update.message.reply_text(msg, parse_mode="HTML")
                else:
                    await update.message.reply_text(
                        "Something went wrong talking to the model. Try again."
                    )
                session["history"].pop()
                return
        except RuntimeError as e:
            log.exception("LLM call failed (provider error)")
            msg = _format_openrouter_runtime_error_for_telegram(
                e,
                model=session.get("model"),
            )
            await update.message.reply_text(msg, parse_mode="HTML")
            session["history"].pop()
            return
        except Exception:
            log.exception("LLM call failed")
            await update.message.reply_text(
                "Something went wrong talking to the model. Try again."
            )
            session["history"].pop()
            return

    assert reply is not None
    if not reply:
        reply = "(empty response)"
    session["history"].append({"role": "assistant", "content": reply})

    if isinstance(user_content, list) and len(session["history"]) >= 2:
        prev = session["history"][-2]
        if prev.get("role") == "user" and prev.get("content") is user_content:
            prev["content"] = (
                memory_user_text
                + "\n\n(Images were attached; the model saw them for this reply only.)"
            )

    await _send_reply(update, reply)

    if fallback_info := response_meta.get("fallback_used"):
        original, fallback = fallback_info
        await update.message.reply_text(
            f"⚠️ Rate limited on <code>{html.escape(original)}</code>; "
            f"response was generated with <code>{html.escape(fallback)}</code>.",
            parse_mode="HTML",
        )

    asyncio.create_task(_background_extract(memory_user_text, reply, chat_model))

    if config.TTS_ENABLED and session["tts"]:
        asyncio.create_task(_background_tts(update, reply))


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    user_text = update.message.text
    if not user_text:
        return

    ts = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M Moscow")
    await _conversation_reply(
        update,
        ctx,
        user_content=f"[Sent at {ts}]\n\n{user_text}",
        memory_user_text=user_text,
        stream=True,
        web_search=session["web_search"],
        allow_fallback=True,
    )


async def _media_group_flush_worker(key: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.sleep(config.MEDIA_GROUP_DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return
    async with _album_lock:
        state = _album_pending.pop(key, None)
    if not state:
        return
    update = state["last_update"]
    items: list[dict] = state["items"]
    try:
        await _handle_image_bundle(ctx, update, items)
    except Exception:
        log.exception("Failed to process media group")
        try:
            await update.message.reply_text(
                "Something went wrong processing your images. Try again or send fewer images."
            )
        except Exception:
            log.exception("Could not send media group error reply")


async def _enqueue_album_item(
    key: str,
    item: dict,
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    async with _album_lock:
        if key not in _album_pending:
            _album_pending[key] = {"items": [], "task": None, "last_update": update}
        st = _album_pending[key]
        st["items"].append(item)
        st["last_update"] = update
        if st["task"] is not None:
            st["task"].cancel()
            st["task"] = None
        st["task"] = asyncio.create_task(_media_group_flush_worker(key, ctx))


async def _handle_image_bundle(
    ctx: ContextTypes.DEFAULT_TYPE,
    update: Update,
    items: list[dict],
) -> None:
    if not _is_allowed_user(update):
        return
    if len(items) > config.MAX_VISION_IMAGES:
        await update.message.reply_text(
            f"Too many images in one batch (max {config.MAX_VISION_IMAGES}). "
            "Send fewer images or split into multiple messages."
        )
        return

    ts = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M Moscow")
    captions: list[str | None] = [it.get("caption") for it in items]
    memory_user_text = _vision_memory_summary(ts, captions)

    content_parts: list[dict] = []
    preamble_lines = [
        f"[Sent at {ts} Moscow]",
        f"The user attached {len(items)} image(s).",
    ]
    for i, it in enumerate(items, start=1):
        cap = (it.get("caption") or "").strip()
        if cap:
            preamble_lines.append(f"Image {i} caption: {cap}")
        else:
            preamble_lines.append(f"Image {i}: (no caption)")
    content_parts.append({"type": "text", "text": "\n".join(preamble_lines)})

    total_bytes = 0
    for idx, it in enumerate(items, start=1):
        file_id = it["file_id"]
        mime = it.get("mime_type") or "image/jpeg"
        try:
            raw = await _download_telegram_file(
                ctx.bot,
                file_id,
                max_bytes=config.MAX_IMAGE_BYTES,
            )
        except ValueError as e:
            log.warning("Image %s rejected: %s", idx, e)
            await update.message.reply_text(
                f"Image {idx} is too large or invalid: {e} "
                f"(max {config.MAX_IMAGE_BYTES // (1024 * 1024)} MB per file)."
            )
            return
        except TelegramError:
            log.exception("Telegram file download failed for image %s", idx)
            await update.message.reply_text(
                f"Could not download image {idx} from Telegram. Try again."
            )
            return
        except Exception:
            log.exception("Unexpected error downloading image %s", idx)
            await update.message.reply_text(
                f"Could not download image {idx}. Try again."
            )
            return
        total_bytes += len(raw)
        if total_bytes > config.MAX_VISION_PAYLOAD_BYTES:
            await update.message.reply_text(
                "Combined image size is too large for one request. "
                "Send fewer or smaller images."
            )
            return
        content_parts.append(_openrouter_image_part(raw, mime))

    await _conversation_reply(
        update,
        ctx,
        user_content=content_parts,
        memory_user_text=memory_user_text,
        stream=False,
        web_search=False,
        allow_fallback=False,
        model=config.VISION_MODEL,
    )


def _media_item_from_message(message) -> dict | None:
    if message.photo:
        return {
            "kind": "photo",
            "file_id": message.photo[-1].file_id,
            "caption": message.caption,
            "mime_type": "image/jpeg",
        }
    if message.document and message.document.mime_type:
        if not str(message.document.mime_type).startswith("image/"):
            return None
        return {
            "kind": "document",
            "file_id": message.document.file_id,
            "caption": message.caption,
            "mime_type": message.document.mime_type,
        }
    return None


async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    message = update.message
    if not message:
        return
    item = _media_item_from_message(message)
    if not item:
        return

    chat = update.effective_chat
    if not chat:
        return

    mg_id = message.media_group_id
    if mg_id is None:
        await _handle_image_bundle(ctx, update, [item])
        return

    key = f"{chat.id}:{mg_id}"
    await _enqueue_album_item(key, item, update, ctx)


async def _telegram_reply(
    message,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    """Send a Telegram message with one retry on transient network errors."""
    for attempt in range(2):
        try:
            if parse_mode:
                await message.reply_text(text, parse_mode=parse_mode)
            else:
                await message.reply_text(text)
            return
        except BadRequest:
            if parse_mode:
                await message.reply_text(text)
                return
            raise
        except TELEGRAM_TRANSIENT_ERRORS:
            if attempt == 0:
                log.warning("Telegram reply timed out; retrying once")
                await asyncio.sleep(1.0)
                continue
            raise


async def _safe_send_action(update: Update) -> None:
    try:
        await update.message.chat.send_action("typing")
    except TELEGRAM_TRANSIENT_ERRORS:
        log.warning("send_chat_action timed out; continuing without typing indicator")
    except TelegramError:
        log.exception("send_chat_action failed; continuing without typing indicator")


async def _handle_app_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled bot error", exc_info=context.error)

    if session["history"] and session["history"][-1].get("role") == "user":
        session["history"].pop()

    if not isinstance(update, Update):
        return
    message = update.effective_message
    if message is None:
        return

    err = context.error
    if isinstance(err, TELEGRAM_TRANSIENT_ERRORS):
        user_msg = "Telegram connection timed out. Please send your message again."
    elif isinstance(err, TelegramError):
        user_msg = "Something went wrong talking to Telegram. Please try again."
    else:
        user_msg = "Something went wrong. Please try again."

    try:
        await _telegram_reply(message, user_msg)
    except Exception:
        log.exception("Could not send error reply to user")


async def _send_draft(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    draft_id: int,
    text: str,
) -> bool:
    """Try Telegram native draft streaming and return whether it remains usable."""
    if not text:
        return True
    try:
        await ctx.bot.do_api_request(
            "sendMessageDraft",
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "text": text[:MAX_TG_LEN],
            },
        )
        return True
    except Exception:
        # Keep this non-fatal: chat should still complete via final send_message.
        log.exception("sendMessageDraft failed; disabling draft streaming for this reply")
        return False


async def _send_reply(update: Update, text: str) -> None:
    """Send text back, handling length limits and HTML parse failures."""
    chunks = [text[i : i + MAX_TG_LEN] for i in range(0, len(text), MAX_TG_LEN)]
    for chunk in chunks:
        chunk_html = _markdown_to_telegram_html(chunk)
        await _telegram_reply(update.message, chunk_html, parse_mode="HTML")


async def _background_extract(user_text: str, assistant_reply: str, chat_model: str) -> None:
    try:
        await memory.extract_and_save(
            AGENT.slug, user_text, assistant_reply, chat_model=chat_model
        )
    except Exception:
        log.exception("Memory extraction failed (non-fatal)")


BOT_COMMANDS = [
    BotCommand("start", "See greeting and commands"),
    BotCommand("new", "Start fresh (cheap=cheap model, max=search on)"),
    BotCommand("search", "Toggle web search (on|off)"),
    BotCommand("remember", "Save a note to memory"),
    BotCommand("memory", "Show what I remember"),
    BotCommand("model", "Switch LLM model"),
    BotCommand("cheap", "Use 3.1 flash lite (cheaper)"),
    BotCommand("read", "Auto-read replies aloud (on|off)"),
    BotCommand("openrouter_balance", "Check OpenRouter remaining credits"),
]


async def _set_commands(application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)
    try:
        pruned = memory.prune_recent(AGENT.slug)
        if pruned:
            log.info("Pruned %d expired recent notes on startup", pruned)
    except Exception:
        log.exception("Startup prune of recent notes failed (non-fatal)")


def _telegram_request() -> HTTPXRequest:
    """HTTP client for Telegram Bot API. Longer timeouts help flaky or proxied paths."""
    kwargs: dict = {
        "read_timeout": 60.0,
        "connect_timeout": 30.0,
        "write_timeout": 30.0,
        "media_write_timeout": 120.0,
    }
    if config.TELEGRAM_HTTP_PROXY:
        kwargs["proxy"] = config.TELEGRAM_HTTP_PROXY
        log.info("Telegram API client using HTTP proxy")
    return HTTPXRequest(**kwargs)


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(_telegram_request())
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("cheap", cmd_cheap))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("openrouter_balance", cmd_openrouter_balance))
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            handle_media,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(_handle_app_error)

    app.post_init = _set_commands

    log.info("Bot starting with agent=%s model=%s", AGENT.name, AGENT.default_model)
    app.run_polling()


if __name__ == "__main__":
    main()
