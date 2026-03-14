from __future__ import annotations

from dataclasses import dataclass

import config


@dataclass
class AgentConfig:
    name: str
    slug: str
    system_prompt_template: str
    default_model: str = config.DEFAULT_MODEL
    extraction_model: str = config.EXTRACTION_MODEL

    def build_system_prompt(self, memory: str) -> str:
        memory_section = memory.strip() if memory.strip() else "(nothing yet)"
        return self.system_prompt_template.replace("{memory}", memory_section)
