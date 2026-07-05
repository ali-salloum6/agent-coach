from agents.base import AgentConfig
from agents.gym_coach import gym_coach

SLUG = "gym_coach_supernova"

SYSTEM_PROMPT = gym_coach.system_prompt_template.replace("gym_coach", SLUG)

gym_coach_supernova = AgentConfig(
    name="Gym Coach",
    slug=SLUG,
    system_prompt_template=SYSTEM_PROMPT,
    memory_writer_context=gym_coach.memory_writer_context,
)
