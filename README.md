# Telegram AI Coach

A Telegram bot that acts as a long-term AI assistant with persistent memory. First agent: **gym coach** (injuries, diet, exercises, programming). Built to be generic so you can add more specialist bots later.

- **Memory**: Remembers (almost) everything you tell it. Stored in `data/<agent>.md`, loaded into the system prompt on each new conversation.
- **OpenRouter**: Any model via OpenRouter (e.g. `/new google/gemini-3.1-flash-lite`). Default: `google/gemini-3.1-pro-preview`.
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
| `/new [model]` | Start a fresh conversation; loads memory. Optional: set model (e.g. `/new google/gemini-3.1-flash-lite`) |
| `/search on\|off` | Turn web search on or off for this conversation |
| `/remember <text>` | Manually add a note to memory |
| `/memory` | Show what the bot remembers |
| `/forget` | Wipe memory (creates a timestamped backup first) |
| `/summarize` | Condense memory (backs up first; uses gemini-3-flash-preview) |
| `/model <slug>` | Change the LLM model mid-conversation |

## How memory works

- **Auto**: After each exchange, a cheap model extracts new facts and appends them to `data/gym_coach.md` (with timestamps).
- **Manual**: `/remember <text>` appends a line to the same file.
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

## Adding another agent

1. Add `agents/<name>.py` with an `AgentConfig` (name, slug, system_prompt_template with `{memory}`).
2. In `config.py`, set `ACTIVE_AGENT` to the new slug (or make agent selectable).
3. In `bot.py`, import and use the new agent instead of `gym_coach`.

Memory is per-agent: each agent gets its own `data/<slug>.md` file.
