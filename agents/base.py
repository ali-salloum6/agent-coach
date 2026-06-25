from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import config

TTS_PROMPT_GUIDANCE = """\
**Read-aloud:** Replies are often read aloud via text-to-speech. Write so they \
sound natural when spoken: spell out units (e.g. "95 kilograms" not "95 kg" or \
"95g"), say "about" instead of "~", avoid bare abbreviations, and use clear \
plain sentences.
"""


@dataclass
class AgentConfig:
    name: str
    slug: str
    system_prompt_template: str
    default_model: str = config.DEFAULT_MODEL
    extraction_model: str = config.EXTRACTION_MODEL
    memory_writer_context: str = "a long-term Telegram coach"

    def build_system_prompt(self, memory: str) -> str:
        memory_section = memory.strip() if memory.strip() else "(nothing yet)"
        now = datetime.now()
        current_datetime = now.strftime("%Y-%m-%d, %A, %H:%M")
        prompt = self.system_prompt_template.replace("{memory}", memory_section).replace(
            "{current_datetime}", current_datetime
        )
        if config.TTS_ENABLED:
            prompt = prompt.rstrip() + "\n\n" + TTS_PROMPT_GUIDANCE
        return prompt
