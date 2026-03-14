from __future__ import annotations

import logging

import httpx

import config

log = logging.getLogger(__name__)


async def chat(
    messages: list[dict],
    model: str = config.DEFAULT_MODEL,
    web_search: bool = False,
) -> str:
    """Send messages to OpenRouter and return the assistant's reply text."""
    body: dict = {
        "model": model,
        "messages": messages,
    }
    if web_search:
        body["plugins"] = [{"id": "web"}]

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(config.OPENROUTER_BASE_URL, json=body, headers=headers)
        if resp.is_error:
            log.error("OpenRouter %s for model=%s: %s", resp.status_code, model, resp.text)
            resp.raise_for_status()
        data = resp.json()

    return data["choices"][0]["message"]["content"]


async def extract_memories(
    user_message: str,
    assistant_response: str,
    existing_memory: str,
) -> str | None:
    """Ask a cheap model to extract new facts from an exchange.

    Returns a markdown bullet list of new facts, or None if nothing new.
    """
    prompt = (
        "Extract any new facts, preferences, goals, or personal details worth "
        "remembering from this exchange. Return ONLY a markdown bullet list of "
        'new facts, or "NONE" if nothing new is worth remembering. Be concise. '
        "Do not repeat known facts.\n\n"
        f"Known facts:\n{existing_memory or '(none yet)'}\n\n"
        f"Exchange:\nUser: {user_message}\nAssistant: {assistant_response}"
    )

    result = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=config.EXTRACTION_MODEL,
    )

    stripped = result.strip()
    if stripped.upper() == "NONE" or not stripped:
        return None
    return stripped


async def summarize_memory(existing_memory: str) -> str:
    """Condense memory into a shorter markdown summary. Uses EXTRACTION_MODEL."""
    prompt = (
        "Summarize this memory file into a shorter markdown document. "
        "Keep all important facts, preferences, goals, and personal details. "
        "Use clear headings and bullet points. Remove redundancy and merge similar items. "
        "Output only the summarized markdown, no preamble.\n\n"
        f"Memory to summarize:\n{existing_memory}"
    )
    result = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=config.EXTRACTION_MODEL,
    )
    return result.strip()
