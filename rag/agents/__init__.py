"""
rag.agents
==========
Multi-agent contract layer (Phase 1.1a — formalization of research-mode).

Importing this package has the side-effect of registering every concrete
agent module imported below. Callers only need ``from rag.agents import
get, register, ...``; the registry is populated by the time the import
returns.
"""

from rag.agents.base import (
    Agent,
    AgentContext,
    AgentResult,
    AgentStatus,
    CapabilityCard,
    all_capabilities,
    get,
    register,
)

# Side-effect imports — each module's bottom registers its agent.
from rag.agents import (  # noqa: F401
    analyst,
    critic,
    orchestrator,
    planner,
    search,
    synthesizer,
)

__all__ = [
    "Agent",
    "AgentContext",
    "AgentResult",
    "AgentStatus",
    "CapabilityCard",
    "all_capabilities",
    "get",
    "register",
]
