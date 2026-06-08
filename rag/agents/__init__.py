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
    CapabilityCard,
    all_capabilities,
    get,
    register,
)

# Side-effect imports — each module's bottom registers its agent.
from rag.agents import analyst, critic, planner, search, synthesizer  # noqa: F401

__all__ = [
    "Agent",
    "CapabilityCard",
    "all_capabilities",
    "get",
    "register",
]
