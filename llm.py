from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

import config

log = logging.getLogger(__name__)


def _assistant_content_to_text(content: object) -> str:
    """Normalize OpenRouter/OpenAI message.content (str or multimodal parts) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def _build_request_body(messages: list[dict], model: str, web_search: bool) -> dict:
    body: dict = {
        "model": model,
        "messages": messages,
    }
    if web_search:
        body["plugins"] = [{"id": "web"}]
    # OpenRouter: maps effort → Google thinkingLevel for Gemini 3.x; skip for non-Google fallbacks.
    if model.startswith("google/"):
        body["reasoning"] = {"effort": "medium"}
    return body


def _request_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }


def _fallback_for_429(model: str, *, allow_fallback: bool) -> str | None:
    """Return fallback model for 429 rate limit, or None."""
    if not allow_fallback:
        return None
    return config.RATE_LIMIT_FALLBACK.get(model)


async def chat(
    messages: list[dict],
    model: str = config.DEFAULT_MODEL,
    web_search: bool = False,
    response_meta: dict | None = None,
    allow_fallback: bool = True,
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
                    and (fallback := _fallback_for_429(current_model, allow_fallback=allow_fallback))
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
            try:
                # OpenAI-style response (what we normally expect from OpenRouter).
                choices = data["choices"]
                raw_content = choices[0]["message"]["content"]
                return _assistant_content_to_text(raw_content)
            except (KeyError, IndexError, TypeError) as e:
                # Providers sometimes return a success HTTP code but an error-shaped JSON
                # that doesn't include `choices` (which would otherwise raise KeyError).
                if isinstance(data, dict):
                    if "error" in data:
                        err = data.get("error") or {}
                        if isinstance(err, dict):
                            msg = (
                                err.get("message")
                                or err.get("type")
                                or err.get("code")
                                or "unknown provider error"
                            )
                        else:
                            msg = str(err)
                        log.error(
                            "OpenRouter error payload for model=%s: %s",
                            current_model,
                            msg,
                        )
                        raise RuntimeError(f"OpenRouter error: {msg}") from e
                    log.error(
                        "OpenRouter response missing choices for model=%s. keys=%s body_preview=%s",
                        current_model,
                        list(data.keys()),
                        resp.text[:500] if resp.text else "",
                    )
                else:
                    log.error(
                        "OpenRouter response not a JSON object for model=%s: type=%s body_preview=%s",
                        current_model,
                        type(data).__name__,
                        resp.text[:500] if resp.text else "",
                    )
                raise RuntimeError(
                    "OpenRouter response missing 'choices' (unexpected format)"
                ) from e


async def get_openrouter_credits() -> dict:
    """
    Query OpenRouter account credits.
    """
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(config.OPENROUTER_CREDITS_URL, headers=headers)
        if resp.is_error:
            log.error(
                "OpenRouter credits %s: %s",
                resp.status_code,
                resp.text[:500] if resp.text else "",
            )
            resp.raise_for_status()
        data = resp.json()
        credits = (data.get("data") or {})
        return {
            "total_credits": credits.get("total_credits"),
            "total_usage": credits.get("total_usage"),
            "raw": data,
        }


async def get_openrouter_key_info() -> dict:
    """
    Query OpenRouter key-scoped usage for the current API key.

    This endpoint is scoped to the exact bearer key used in Authorization.
    """
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(config.OPENROUTER_KEY_INFO_URL, headers=headers)
        if resp.is_error:
            log.error(
                "OpenRouter key info %s: %s",
                resp.status_code,
                resp.text[:500] if resp.text else "",
            )
            resp.raise_for_status()
        data = resp.json()
        payload = data.get("data") or {}
        return {
            "label": payload.get("label"),
            "usage": payload.get("usage"),
            "usage_daily": payload.get("usage_daily"),
            "usage_weekly": payload.get("usage_weekly"),
            "usage_monthly": payload.get("usage_monthly"),
            "limit": payload.get("limit"),
            "limit_reset": payload.get("limit_reset"),
            "limit_remaining": payload.get("limit_remaining"),
            "is_free_tier": payload.get("is_free_tier"),
            "is_management_key": payload.get("is_management_key"),
            "raw": data,
        }


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
                if not content:
                    continue
                if isinstance(content, str):
                    yield content
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "text":
                            continue
                        text = block.get("text")
                        if text:
                            yield text


async def chat_stream(
    messages: list[dict],
    model: str = config.DEFAULT_MODEL,
    web_search: bool = False,
    response_meta: dict | None = None,
    allow_fallback: bool = True,
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
                and (fallback := _fallback_for_429(current_model, allow_fallback=allow_fallback))
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
        "Memory mode: conservative with correct attribution.\n\n"
        "Extract new memories worth remembering from this exchange.\n"
        "You must save BOTH:\n"
        "1) USER-stated facts (what the user reports/does/decides) as USER items.\n"
        "2) ASSISTANT recommendations as ASSISTANT advice items.\n\n"
        "Critical rules (must follow):\n"
        "- Never convert ASSISTANT advice into USER decisions.\n"
        "- Never use USER-decision language for ASSISTANT items (do not write 'adopted/decided/user decided' for assistant advice).\n"
        "- USER item is allowed only if the USER explicitly reports it in first person (state/symptom/event/preference/plan) OR explicitly commits/action-requests.\n"
        "  Accept phrases like: 'My X feels...', 'I’m experiencing...', 'I feel...', 'I have...', 'I did...', 'I decided...', 'I will...', 'I stopped...', 'I’m going to...'.\n"
        "- ASSISTANT item is allowed only if the ASSISTANT is recommending/suggesting/advising something; always label it as assistant advice.\n"
        "- If attribution is unclear or the user did not confirm adoption, omit the USER decision and keep only the ASSISTANT advice (if any).\n"
        "- If something happened once, record it as a one-time event (with date if present), not as an ongoing habit.\n"
        "- Do NOT generalize from a single event by using words like 'typically', 'usually', 'often', 'regularly' unless clearly supported by multiple entries.\n"
        "- Do not repeat known facts.\n\n"
        "Output format:\n"
        "- Return ONLY a markdown bullet list of new items, or \"NONE\" if nothing new.\n"
        "- Each bullet must start with either 'USER:' or 'ASSISTANT:'\n"
        "- Keep each bullet concise.\n\n"
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
    """Condense memory into a shorter markdown summary."""
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
        model=config.SUMMARIZATION_MODEL,
        allow_fallback=False,
    )
    return result.strip()
