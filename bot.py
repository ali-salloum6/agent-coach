from __future__ import annotations

import asyncio
import logging

from telegram import Update
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
from agents.gym_coach import gym_coach

MAX_TG_LEN = 4096

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

AGENT = gym_coach

session: dict = {
    "model": AGENT.default_model,
    "history": [],
    "web_search": False,
}


def _reset_session(model: str | None = None) -> None:
    mem = memory.load(AGENT.slug)
    system_prompt = AGENT.build_system_prompt(mem)
    session["model"] = model or AGENT.default_model
    session["history"] = [{"role": "system", "content": system_prompt}]
    session["web_search"] = False


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _reset_session()
    await update.message.reply_text(
        f"Hey! I'm your *{AGENT.name}*.\n\n"
        "Commands:\n"
        "/new [model] — start a fresh conversation\n"
        "/search on|off — toggle web search\n"
        "/remember <text> — save a note to memory\n"
        "/memory — show what I remember\n"
        "/forget — wipe memory (backs up first)\n"
        "/model <slug> — switch LLM model\n"
        "/cheap — use 3.1 flash lite (cheaper)\n\n"
        f"Current model: `{session['model']}`",
        parse_mode="Markdown",
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    model = " ".join(ctx.args) if ctx.args else None
    _reset_session(model)
    await update.message.reply_text(
        f"Fresh conversation started.\nModel: `{session['model']}`",
        parse_mode="Markdown",
    )


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        status = "on" if session["web_search"] else "off"
        await update.message.reply_text(f"Web search is *{status}*.", parse_mode="Markdown")
        return
    flag = ctx.args[0].lower()
    if flag == "on":
        session["web_search"] = True
        await update.message.reply_text("Web search *enabled* for this conversation.", parse_mode="Markdown")
    elif flag == "off":
        session["web_search"] = False
        await update.message.reply_text("Web search *disabled*.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: /search on|off")


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text("Usage: /remember <something to remember>")
        return
    memory.append(AGENT.slug, text)
    await update.message.reply_text(f"Noted: _{text}_", parse_mode="Markdown")


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    mem = memory.load(AGENT.slug)
    if not mem.strip():
        await update.message.reply_text("Memory is empty.")
        return
    # Telegram messages max out at 4096 chars
    if len(mem) > 4000:
        mem = mem[:4000] + "\n\n…(truncated)"
    await update.message.reply_text(mem)


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    backup = memory.forget(AGENT.slug)
    if backup:
        await update.message.reply_text("Memory wiped. A backup was saved.")
    else:
        await update.message.reply_text("Nothing to forget — memory was already empty.")


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text(
            f"Current model: `{session['model']}`",
            parse_mode="Markdown",
        )
        return
    session["model"] = ctx.args[0]
    await update.message.reply_text(
        f"Model switched to `{session['model']}`",
        parse_mode="Markdown",
    )


CHEAP_MODEL = "google/gemini-3.1-flash-lite-preview"


async def cmd_cheap(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    session["model"] = CHEAP_MODEL
    await update.message.reply_text(
        f"Switched to cheap model: `{CHEAP_MODEL}`",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text
    if not user_text:
        return

    session["history"].append({"role": "user", "content": user_text})

    await update.message.chat.send_action("typing")

    try:
        reply = await llm.chat(
            messages=session["history"],
            model=session["model"],
            web_search=session["web_search"],
        )
    except Exception:
        log.exception("LLM call failed")
        await update.message.reply_text("Something went wrong talking to the model. Try again.")
        session["history"].pop()
        return

    session["history"].append({"role": "assistant", "content": reply})

    await _send_reply(update, reply or "(empty response)")

    asyncio.create_task(_background_extract(user_text, reply or ""))


async def _send_reply(update: Update, text: str) -> None:
    """Send text back, handling length limits and markdown parse failures."""
    chunks = [text[i : i + MAX_TG_LEN] for i in range(0, len(text), MAX_TG_LEN)]
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except BadRequest:
            await update.message.reply_text(chunk)


async def _background_extract(user_text: str, assistant_reply: str) -> None:
    try:
        await memory.extract_and_save(AGENT.slug, user_text, assistant_reply)
    except Exception:
        log.exception("Memory extraction failed (non-fatal)")


def main() -> None:
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("cheap", cmd_cheap))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot starting with agent=%s model=%s", AGENT.name, AGENT.default_model)
    app.run_polling()


if __name__ == "__main__":
    main()
