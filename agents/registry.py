from __future__ import annotations

from agents.base import AgentConfig
from agents.german_exam import german_exam
from agents.gym_coach import gym_coach
from agents.gym_coach_supernova import gym_coach_supernova
from agents.life_coach import life_coach

AGENT_REGISTRY: dict[str, AgentConfig] = {
    gym_coach.slug: gym_coach,
    gym_coach_supernova.slug: gym_coach_supernova,
    german_exam.slug: german_exam,
    life_coach.slug: life_coach,
}


def get_agent(slug: str) -> AgentConfig:
    return AGENT_REGISTRY.get(slug, gym_coach)
