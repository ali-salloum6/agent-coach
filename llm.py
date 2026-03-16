from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

import config

log = logging.getLogger(__name__)


def _build_request_body(messages: list[dict], model: str, web_search: bool) -> dict:
    body: dict = {
        "model": model,
        "messages": messages,
    }
    if web_search:
        body["plugins"] = [{"id": "web"}]
    return body


def _request_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }


def _fallback_for_429(model: str) -> str | None:
    """Return fallback model for 429 rate limit, or None."""
    return config.RATE_LIMIT_FALLBACK.get(model)


async def chat(
    messages: list[dict],
    model: str = config.DEFAULT_MODEL,
    web_search: bool = False,
    response_meta: dict | None = None,
) -> str:
    """Send messages to OpenRouter and return the assistant's reply text.
    If response_meta is provided and a 429 fallback was used, sets
    response_meta["fallback_used"] = (original_model, fallback_model).
    """
    headers = _request_headers()
    current_model = model
    requested_model = model

    async with httpx.AsyncClient(timeout=120) as client:
        while True:
            body = _build_request_body(messages, current_model, web_search)
            resp = await client.post(config.OPENROUTER_BASE_URL, json=body, headers=headers)
            if resp.is_error:
                log.error(
                    "OpenRouter %s for model=%s: %s",
                    resp.status_code,
                    current_model,
                    resp.text,
                )
                if (
                    resp.status_code == 429
                    and (fallback := _fallback_for_429(current_model))
                ):
                    log.warning(
                        "Rate limited on %s, retrying with %s",
                        current_model,
                        fallback,
                    )
                    if response_meta is not None:
                        response_meta["fallback_used"] = (requested_model, fallback)
                    current_model = fallback
                    continue
                resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


async def _chat_stream_attempt(
    messages: list[dict],
    model: str,
    web_search: bool,
) -> AsyncIterator[str]:
    """Single attempt at streaming; may raise httpx.HTTPStatusError on 429."""
    body = _build_request_body(messages, model, web_search)
    body["stream"] = True
    headers = _request_headers()

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST", config.OPENROUTER_BASE_URL, json=body, headers=headers
        ) as resp:
            if resp.is_error:
                error_text = await resp.aread()
                error_text_decoded = error_text.decode(errors="replace")
                log.error(
                    "OpenRouter stream %s for model=%s: %s",
                    resp.status_code,
                    model,
                    error_text_decoded,
                )
                resp.raise_for_status()

            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    continue

                payload = line[6:].strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    break

                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if "error" in chunk:
                    message = chunk["error"].get("message", "unknown stream error")
                    raise RuntimeError(f"OpenRouter stream error: {message}")

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content


async def chat_stream(
    messages: list[dict],
    model: str = config.DEFAULT_MODEL,
    web_search: bool = False,
    response_meta: dict | None = None,
) -> AsyncIterator[str]:
    """Stream text deltas from OpenRouter chat completions SSE endpoint.
    On 429 rate limit, retries once with the configured fallback model.
    If response_meta is provided and fallback was used, sets
    response_meta["fallback_used"] = (original_model, fallback_model).
    """
    current_model = model
    requested_model = model
    fallback_used = False

    while True:
        try:
            async for delta in _chat_stream_attempt(
                messages, current_model, web_search
            ):
                yield delta
            return
        except httpx.HTTPStatusError as e:
            if (
                e.response.status_code == 429
                and not fallback_used
                and (fallback := _fallback_for_429(current_model))
            ):
                log.warning(
                    "Rate limited on %s, retrying stream with %s",
                    current_model,
                    fallback,
                )
                if response_meta is not None:
                    response_meta["fallback_used"] = (requested_model, fallback)
                current_model = fallback
                fallback_used = True
            else:
                raise


async def extract_memories(
    user_message: str,
    assistant_response: str,
    existing_memory: str,
) -> str | None:
    """Ask a cheap model to extract new facts from an exchange.

    Returns a markdown bullet list of new facts, or None if nothing new.
    """
    prompt = (
        "Extract new facts worth remembering from this exchange. Save:\n"
        "- What the user said: their situation, status, preferences, goals, what they did or shared.\n"
        "- When the user asks a factual question about something they did (e.g. 'how much X in what I ate/did?'), "
        "save the answer as a fact about the user (e.g. 'User had X amount of Y').\n"
        "Do NOT save the assistant's advice or recommendations—only facts that describe the user's actual situation.\n"
        "- If something happened once, record it as a one-time event (with date if present), not as an ongoing habit.\n"
        "- Do NOT generalize from a single event by using words like 'typically', 'usually', 'often', or 'regularly' "
        "unless the user explicitly described it that way or there is clear evidence from multiple past entries.\n"
        "Return ONLY a markdown bullet list of new facts, or \"NONE\" if nothing new. Be concise. "
        "If you should save a suggestion from the assistant, do not save it as a fact—note that it was suggested. "
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
    """Condense memory into a shorter markdown summary. Uses EXTRACTION_MODEL (gemini-3-flash-preview)."""
    prompt = (
        "Summarize this memory file into a shorter markdown document. "
        "Keep all important facts, preferences, goals, and personal details. "
        "Use clear headings and bullet points. Remove redundancy and merge similar items. "
        "Preserve timestamps: keep at least the date (YYYY-MM-DD) for when things were noted. "
        "You may combine multiple items from the same day under one date; do not strip dates entirely.\n"
        "Output only the summarized markdown, no preamble.\n\n"
        f"Memory to summarize:\n{existing_memory}"
    )
    result = await chat(
        messages=[{"role": "user", "content": prompt}],
        model=config.EXTRACTION_MODEL,
    )
    return result.strip()
