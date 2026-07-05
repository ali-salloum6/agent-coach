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


def _reasoning_for_model(model: str) -> dict | None:
    if model.startswith("z-ai/glm-"):
        return {"effort": "max"}
    if model.startswith("google/"):
        return {"effort": "medium"}
    return None


def _build_request_body(messages: list[dict], model: str, web_search: bool) -> dict:
    body: dict = {
        "model": model,
        "messages": messages,
    }
    if web_search:
        body["plugins"] = [{"id": "web"}]
    if reasoning := _reasoning_for_model(model):
        body["reasoning"] = reasoning
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
    timeout_seconds: float = 120.0,
) -> str:
    """Send messages to OpenRouter and return the assistant's reply text.
    If response_meta is provided and a 429 fallback was used, sets
    response_meta["fallback_used"] = (original_model, fallback_model).
    `timeout_seconds` controls the per-request httpx timeout (default 120s;
    bumped to ~300s for the structured memory writer where Gemini Pro can
    take longer when reasoning is medium/high).
    """
    headers = _request_headers()
    current_model = model
    requested_model = model

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
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


async def propose_memory_ops(
    *,
    user_message: str,
    assistant_response: str,
    current_snapshot: str,
    active_issues: str,
    active_plans: str,
    recent_memory_visible: str,
    timestamp_iso: str,
    model: str,
    writer_context: str = "a long-term Telegram coach",
) -> str:
    """Ask the writer model to propose strict JSON memory operations.

    Returns the raw model text. The caller is responsible for validating it
    via `memory_ops.validate(...)` — this function never mutates files.

    Input deliberately excludes the full memory file: the validation dry
    run showed that giving the writer huge context bloats cost and biases
    it toward unsafe long-term plans. The answering model still receives
    full curated memory; only the writer is kept narrow.
    """
    schema_block = (
        "Allowed operations (return JSON object {\"ops\": [...]}; an empty list or"
        " a single {\"op\":\"noop\"} both mean nothing worth saving):\n"
        "- noop: nothing worth saving.\n"
        "- add_recent_note: ephemeral facts useful today or this week only.\n"
        "- add_measurement: dated numeric/semi-numeric observation (weight, sleep,"
        " calories, protein, pain level, waist, workout result).\n"
        "- add_current_fact: explicit current state, goal, constraint, injury,"
        " routine, supplement, or durable preference stated by the user.\n"
        "- add_event: meaningful dated event or milestone.\n"
        "- add_plan: concrete assistant recommendation/protocol/plan.\n"
        "- mark_adopted: user explicitly says they followed/adopted a previous plan."
        " Provide target_ref quoting the existing plan text.\n"
        "- supersede: user explicitly corrects or overrides a prior current state or"
        " plan. Provide target_ref quoting the old item.\n"
    )

    required_fields = (
        "Required fields for every non-noop op:\n"
        "- op\n"
        "- source: \"user\" or \"assistant\"\n"
        "- date: YYYY-MM-DD if known, otherwise null\n"
        "- summary: ONE atomic claim, max ~280 chars; do not bundle several facts\n"
        "- evidence_quote: EXACT substring from the claimed source text\n"
        "- retention: \"recent\", \"long_term\", or \"historical\"\n"
        "- category: one of profile, goal, weight, sleep, nutrition, workout,"
        " injury, pain, supplement, skin, measurement, mental_health, preference,"
        " logistics, habit, rationale, milestone\n"
        "Optional: value, unit, ttl_days (2|7|14|30), status"
        " (proposed|active|adopted|superseded), target_ref."
    )

    rules = (
        "Strict rules (the host will reject violations):\n"
        "- source=\"user\" requires evidence_quote to be an exact substring of the USER MESSAGE.\n"
        "- source=\"assistant\" requires evidence_quote to be an exact substring of the ASSISTANT REPLY.\n"
        "- Never produce add_current_fact, mark_adopted, or supersede with source=assistant.\n"
        "- Each op is ATOMIC: one claim per op. Split bundled facts into separate ops.\n"
        "- Assistant plans for \"today/tonight/this evening/right now/this session\""
        " are recent (ttl_days=2 or 7). Long-term plans are reserved for durable"
        " rules/protocols (crisis protocol, injury safety rule, recurring supplement"
        " protocol, exercise substitution rule, user-requested durable guidance).\n"
        "- Exact meal items, restaurant/menu browsing, transient location/logistics"
        " are recent. Daily totals and measurements are long-term.\n"
        "- Do NOT save generic explanations or pep talk.\n"
        "- Do NOT infer facts that are not explicitly stated.\n"
        "- If you are uncertain, choose noop or recent over long_term.\n"
        f"- Return at most {5} non-noop operations."
    )

    prompt = (
        f"You are the structured memory writer for {writer_context}.\n"
        "Decide what (if anything) is worth saving from one user/assistant exchange.\n"
        "Return JSON ONLY in the form {\"ops\": [...]}; no prose, no fences.\n\n"
        f"Current timestamp (Moscow): {timestamp_iso}\n\n"
        f"{schema_block}\n"
        f"{required_fields}\n\n"
        f"{rules}\n\n"
        "Existing memory excerpts (treat as authoritative; avoid duplicating):\n"
        "=== Current Snapshot ===\n"
        f"{current_snapshot.strip() or '(empty)'}\n"
        "=== Active Issues ===\n"
        f"{active_issues.strip() or '(empty)'}\n"
        "=== Active Plans ===\n"
        f"{active_plans.strip() or '(empty)'}\n"
        "=== Non-expired Recent Notes ===\n"
        f"{recent_memory_visible.strip() or '(empty)'}\n\n"
        "USER MESSAGE:\n"
        f"{user_message}\n\n"
        "ASSISTANT REPLY:\n"
        f"{assistant_response}\n"
    )

    return await chat(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        allow_fallback=False,
        # Gemini Pro with medium reasoning can take 60-180s on the
        # occasional turn; give it real headroom for the writer path.
        timeout_seconds=300.0,
    )
