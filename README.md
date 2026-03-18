# Telegram AI Coach

A Telegram bot that acts as a long-term AI assistant with persistent memory. First agent: **gym coach** (injuries, diet, exercises, programming). Built to be generic so you can add more specialist bots later.

- **Memory**: Remembers (almost) everything you tell it. Stored in `data/<agent>.md`, loaded into the system prompt on each new conversation. Timestamps in memory are **Moscow time** (Europe/Moscow).
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
   pip install -r requirements.txt
   ```

3. **Configure env**:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and set:

   - `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `OPENROUTER_API_KEY` — from [OpenRouter](https://openrouter.ai/)

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
| `/forget` | Wipe memory (creates a timestamped backup first) |
| `/summarize` | Condense memory (backs up first; uses gemini-3.1-pro-preview; no fallback) |
| `/model <slug>` | Switch model: `mid` / `cheap` / `max`, or a full OpenRouter model slug |
| `/cheap` | Shortcut to switch to the cheap model (gemini-3.1-flash-lite) |

## How memory works

- **Auto**: After each exchange, the extraction model (gemini-3.1-flash-preview; falls back to z-ai/glm-5 on 429) extracts new facts and appends them to `data/<agent>.md` with timestamps in **Moscow time**.
- **Manual**: `/remember <text>` appends a timestamped line to the same file (also Moscow time).
- On `/new`, the full memory file is injected into the **system prompt** under “What you remember about the user”, so the coach has full context every new chat.

## Project layout

```
agent-coach/
  bot.py              # Telegram entry point + command handlers
  config.py            # Env, defaults, data dir
  llm.py               # OpenRouter HTTP client (chat + memory extraction)
  memory.py            # Read/write/extract memory (markdown files)
  agents/
    base.py            # AgentConfig dataclass
    gym_coach.py       # Gym coach persona + system prompt
  data/                # Created at runtime (git-ignored)
    gym_coach.md       # Persistent memory
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
