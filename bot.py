from __future__ import annotations

import asyncio
import html
import itertools
import logging
import re
import time

from telegram import BotCommand, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telegram.error import BadRequest

import config
import llm
import memory
from agents.german_exam import german_exam
from agents.gym_coach import gym_coach

MAX_TG_LEN = 4096


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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

AGENT_REGISTRY = {
    gym_coach.slug: gym_coach,
    german_exam.slug: german_exam,
}

AGENT = AGENT_REGISTRY.get(config.ACTIVE_AGENT, gym_coach)

session: dict = {
    "model": AGENT.default_model,
    "history": [],
    "web_search": False,
}

STREAM_FLUSH_INTERVAL_SECONDS = 0.25
STREAM_FLUSH_MIN_CHARS = 24
_draft_id_counter = itertools.count(start=int(time.time()))


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
    mem = memory.load(AGENT.slug)
    system_prompt = AGENT.build_system_prompt(mem)
    session["model"] = model or AGENT.default_model
    session["history"] = [{"role": "system", "content": system_prompt}]
    session["web_search"] = False


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
        "/forget — wipe memory (backs up first)\n"
        "/summarize — condense memory (backs up first)\n"
        "/model &lt;slug&gt; — switch LLM model\n"
        "/cheap — use 3.1 flash lite (cheaper)\n\n"
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
    # Telegram messages max out at 4096 chars
    if len(mem) > 4000:
        mem = mem[:4000] + "\n\n…(truncated)"
    mem_html = _markdown_to_telegram_html(mem)
    try:
        await update.message.reply_text(mem_html, parse_mode="HTML")
    except BadRequest:
        await update.message.reply_text(mem)


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    backup = memory.forget(AGENT.slug)
    if backup:
        await update.message.reply_text("Memory wiped. A backup was saved.")
    else:
        await update.message.reply_text("Nothing to forget — memory was already empty.")


async def cmd_summarize(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    await update.message.reply_text("Summarizing memory…")
    try:
        ok, msg = await memory.summarize(AGENT.slug)
        await update.message.reply_text(msg)
    except Exception:
        log.exception("Summarize failed")
        await update.message.reply_text("Summarization failed. Your memory was not changed.")


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


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed_user(update):
        return
    user_text = update.message.text
    if not user_text:
        return

    session["history"].append({"role": "user", "content": user_text})

    await update.message.chat.send_action("typing")

    reply_parts: list[str] = []
    chat_id = update.effective_chat.id if update.effective_chat else None
    can_stream_draft = (
        chat_id is not None
        and update.effective_chat is not None
        and update.effective_chat.type == "private"
    )
    draft_id = next(_draft_id_counter)
    loop = asyncio.get_running_loop()
    last_flush_at = loop.time()
    pending_chars = 0
    response_meta: dict = {}

    try:
        async for delta in llm.chat_stream(
            messages=session["history"],
            model=session["model"],
            web_search=session["web_search"],
            response_meta=response_meta,
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

    except Exception:
        if not reply_parts:
            log.exception("LLM stream failed, trying non-stream fallback")
            try:
                reply = await llm.chat(
                    messages=session["history"],
                    model=session["model"],
                    web_search=session["web_search"],
                    response_meta=response_meta,
                )
            except Exception:
                log.exception("LLM call failed")
                await update.message.reply_text("Something went wrong talking to the model. Try again.")
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

    if not reply:
        reply = "(empty response)"
    session["history"].append({"role": "assistant", "content": reply})

    await _send_reply(update, reply)

    if fallback_info := response_meta.get("fallback_used"):
        original, fallback = fallback_info
        await update.message.reply_text(
            f"⚠️ Rate limited on <code>{html.escape(original)}</code>; "
            f"response was generated with <code>{html.escape(fallback)}</code>.",
            parse_mode="HTML",
        )

    asyncio.create_task(_background_extract(user_text, reply))


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
        try:
            await update.message.reply_text(chunk_html, parse_mode="HTML")
        except BadRequest:
            await update.message.reply_text(chunk)


async def _background_extract(user_text: str, assistant_reply: str) -> None:
    try:
        await memory.extract_and_save(AGENT.slug, user_text, assistant_reply)
    except Exception:
        log.exception("Memory extraction failed (non-fatal)")


BOT_COMMANDS = [
    BotCommand("start", "See greeting and commands"),
    BotCommand("new", "Start fresh (cheap=cheap model, max=search on)"),
    BotCommand("search", "Toggle web search (on|off)"),
    BotCommand("remember", "Save a note to memory"),
    BotCommand("memory", "Show what I remember"),
    BotCommand("forget", "Wipe memory (backs up first)"),
    BotCommand("summarize", "Condense memory (backs up first)"),
    BotCommand("model", "Switch LLM model"),
    BotCommand("cheap", "Use 3.1 flash lite (cheaper)"),
]


async def _set_commands(application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def main() -> None:
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("summarize", cmd_summarize))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("cheap", cmd_cheap))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.post_init = _set_commands

    log.info("Bot starting with agent=%s model=%s", AGENT.name, AGENT.default_model)
    app.run_polling()


if __name__ == "__main__":
    main()
