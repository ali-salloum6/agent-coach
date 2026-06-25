# Telegram AI Coach

A Telegram bot that acts as a long-term AI assistant with persistent memory. First agent: **gym coach** (injuries, diet, exercises, programming). Built to be generic so you can add more specialist bots later.

- **Memory**: Two-tier and auto-pruning. Curated long-term memory lives in `data/<agent>.md` and short-lived recent notes in `data/<agent>.recent.md`. After every reply, a **structured memory writer** asks Gemini for strict JSON operations (`add_measurement`, `add_current_fact`, `add_plan`, `add_recent_note`, `supersede`, …) and only writes ops that pass deterministic validation (evidence quote must be an exact substring of the claimed source, categories must be in the allowed set, max 5 ops per exchange). Recent notes auto-expire (default 2–7 days). Timestamps in memory are **Moscow time** (Europe/Moscow). An audit log of every accepted/rejected op is appended to `data/<agent>.memory_ops.jsonl`.
- **OpenRouter**: Model shortcuts: `mid` = gemini-3-flash, `cheap` = gemini-3.1-flash-lite, `max` = default (pro). Or use a full slug (e.g. `/new google/gemini-3.1-flash-lite`). Default: `google/gemini-3.1-pro-preview`.
- **Web search**: Toggle per conversation with `/search on|off` (uses OpenRouter’s web plugin).

## Setup

1. **Clone / open the project** and create a virtualenv (recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # or `.venv\Scripts\activate` on Windows
   ```

2. **Install dependencies**:

   ```bash
   pip install torch --index-url https://download.pytorch.org/whl/cpu
   pip install -r requirements.txt
   ```

   On Debian/Ubuntu, install **espeak-ng** (required by Kokoro for English G2P):

   ```bash
   sudo apt install espeak-ng
   ```

   The first `/read` downloads Kokoro model weights (~300 MB) and spaCy `en_core_web_sm` automatically.

3. **Configure env**:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and set:

   - `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `OPENROUTER_API_KEY` — from [OpenRouter](https://openrouter.ai/)
   - `TTS_VOICE` — optional; Kokoro voice for `/read` (default `bm_george`; American: `am_michael`, `am_fenrir`, …)
   - `TTS_ENABLED` — optional; set to `false` to disable `/read` (default `true`)

4. **Run the bot**:

   ```bash
   python bot.py
   ```

## Commands

| Command | Description |
|--------|-------------|
| `/start` | Welcome message and command list |
| `/new [model\|cheap\|mid\|max]` | Start a fresh conversation; loads memory. Optional: `cheap` / `mid` / `max` (max = default model + web search on), or a full OpenRouter model slug |
| `/search on\|off` | Turn web search on or off for this conversation |
| `/remember <text>` | Manually add a note to memory |
| `/memory` | Show what the bot remembers |
| `/model <slug>` | Switch model: `mid` / `cheap` / `max`, or a full OpenRouter model slug |
| `/cheap` | Shortcut to switch to the cheap model (gemini-3.1-flash-lite) |
| `/read` | Text-to-speech: read the bot’s last reply aloud (Kokoro-82M) |
| `/openrouter_balance` | Check usage and remaining limit for the current `OPENROUTER_API_KEY` |

## How memory works

- **Auto (structured writer)**: After every reply, `memory_writer.propose_and_apply()` calls Gemini for JSON memory operations, validates each one (op type, source, retention, evidence quote, category, atomicity, plan-transience), and routes accepted operations to:
  - `## Time Series > <category>` in `data/<agent>.md` for measurements,
  - `## Current Snapshot`, `## Active Issues`, `## Plans, Advice, And Rationale`, `## Timeline` for durable items,
  - `data/<agent>.recent.md` for short-lived `add_recent_note` entries with a TTL of 2/7/14/30 days (default 2 for one-off details, 7 for short-term context). Same-day assistant plans referencing "today", "tonight", "this evening", "right now" are automatically downgraded from `long_term` to `recent` retention.
- **Auto-expiration**: Expired recent notes are pruned **automatically** — on bot startup, on every `/new`, and whenever memory is loaded for the prompt. There is no manual review.
- **Manual**: `/remember <text>` appends a timestamped note under `## Manual Notes`.
- On `/new`, canonical memory plus non-expired recent notes are injected into the **system prompt** under "What you remember about the user".
- **Audit**: Every accepted/rejected operation is recorded in `data/<agent>.memory_ops.jsonl` for debugging.
- **Rollback**: Set `MEMORY_WRITER_MODE=legacy` to fall back to the old free-form extractor temporarily.

### Seeding from a Telegram export

If you already have an MTProto export at `exports/telegram_chat/messages.jsonl` (produced by `scripts/export_telegram_chat.py`), convert it to a readable Markdown seed without re-exporting:

```bash
python scripts/telegram_jsonl_to_md.py
# writes exports/telegram_chat_md/gym_coach_seed.md and an index
```

This seed becomes the starting point for `data/<agent>.md` after the architecture update — there is no need to keep raw history inside the bot; the export is the authoritative raw source of truth.

## Project layout

```
agent-coach/
  bot.py                # Telegram entry point + command handlers
  config.py             # Env, defaults, data dir, MEMORY_WRITER_MODE, TTS settings
  llm.py                # OpenRouter HTTP client + propose_memory_ops
  memory.py             # Memory facade (load, append, extract_and_save)
  memory_writer.py      # Structured writer: validate, apply, audit, prune
  memory_ops.py         # Strict JSON operation schema + validator
  memory_template.py    # Canonical Markdown layout and recent-entry format
  tts.py                # Markdown cleaning + Kokoro TTS for /read
  agents/
    base.py             # AgentConfig dataclass
    gym_coach.py        # Gym coach persona + system prompt
  scripts/
    export_telegram_chat.py     # MTProto JSONL export
    telegram_jsonl_to_md.py     # JSONL → readable Markdown seed
    validate_memory_writer_ops.py  # Offline dry-run against sampled exchanges
  data/                 # Created at runtime (git-ignored)
    gym_coach.md           # Curated long-term memory
    gym_coach.recent.md    # Auto-expiring recent notes
    gym_coach.memory_ops.jsonl  # Accepted/rejected op audit log
```

## Adding another agent / running multiple bots

- Each agent has:
  - an `AgentConfig` in `agents/<name>.py` (name, slug, system_prompt_template with `{memory}`),
  - its own memory file `data/<slug>.md`.
- The active agent is selected via the `ACTIVE_AGENT` env var (defaults to `gym_coach`).

To add a new agent:

1. Add `agents/<name>.py` with an `AgentConfig` (see `agents/gym_coach.py` or `agents/german_exam.py`).
2. Set `ACTIVE_AGENT=<slug>` in your `.env` (or systemd `EnvironmentFile`) for that bot instance.

To run multiple bots on the same server (e.g. one gym coach, one German exam coach):

1. Create two env files:
   - `.env.gym` with `TELEGRAM_BOT_TOKEN=...`, `OPENROUTER_API_KEY=...`, `ACTIVE_AGENT=gym_coach`, etc.
   - `.env.german` with `TELEGRAM_BOT_TOKEN=...`, `OPENROUTER_API_KEY=...`, `ACTIVE_AGENT=german_exam`, etc.
2. Create two `systemd` services, both pointing to `bot.py` in the same repo, but each using a different `EnvironmentFile` (e.g. `/etc/systemd/system/agent-gym.service` and `/etc/systemd/system/agent-german.service`).
3. `systemctl daemon-reload && systemctl enable --now agent-gym.service agent-german.service`.

Each service will use its own Telegram token, allowed user, and agent persona, while sharing the same codebase.
