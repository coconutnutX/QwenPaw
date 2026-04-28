# -*- coding: utf-8 -*-
"""oG-Memory context manager for QwenPaw agents.

Setup (assuming QwenPaw and oG-Memory service are already installed):
  1. Set ``context_manager_backend`` and ``memory_manager_backend`` both to
     ``"ogmem"`` in ``~/.qwenpaw/workspaces/<agent_id>/agent.json``,
     under the ``"running"`` section.
  2. Set environment variables (optional, defaults shown):
       OGMEMORY_URL=http://localhost:8090
       OGMEMORY_ACCOUNT_ID=default
       OGMEMORY_USER_ID=<agent_id>
  3. Start oG-Memory service (port 8090 by default).
  4. Start QwenPaw.

Verify via logs — look for these markers:
  [ogmem] session started:<id>          — mm bootstrapped ok
  [ogmem-ctx] cached memory_manager     — ctx linked to mm
  [ogmem] POST /api/v1/compose          — memory retrieval
  [ogmem] POST /api/v1/after_turn       — message persistence

Architecture:
  Inherits BaseContextManager
  Does NOT own session_id or HTTP client — all ogmem interaction
  goes through agent.memory_manager (OGMemoryMemoryManager)

  pre_reply()       → no-op
  pre_reasoning()   → mm.retrieve()  → POST /compose
  post_acting()     → mm.after_turn() → POST /after_turn
  post_reply()      → mm.after_turn() → POST /after_turn
  compact_context() → mm.compact()    → POST /compact
"""
import logging
from typing import Any

from agentscope.message import Msg

from ...config.config import load_agent_config
from ..utils.token_counter import get_token_counter
from .agent_context import AgentContext
from .base_context_manager import BaseContextManager, context_registry

logger = logging.getLogger(__name__)


@context_registry.register("ogmem")
class OGMemoryContextManager(BaseContextManager):
    """Context manager backed by oG-Memory via memory_manager delegation.

    Lazily caches agent.memory_manager on first hook call that needs it.
    This avoids modifying workspace.py — the alternative would be injecting
    memory_manager via workspace's post_init callback, which couples
    ogmem-specific logic into the generic workspace layer.
    """

    def __init__(self, working_dir: str, agent_id: str):
        super().__init__(working_dir=working_dir, agent_id=agent_id)
        self.ogmem_mm = None       # lazily cached from agent.memory_manager
        self._last_sent_idx = 0    # track messages already sent to ogmem

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ogmem_mm(self, agent: Any):
        """Return cached OGMemoryMemoryManager, lazily resolved from agent.

        Resolves agent.memory_manager once, logs a warning if it's not
        OGMemoryMemoryManager.  Callers wrapped in try/except handle the
        None case gracefully.
        """
        if self.ogmem_mm is None:
            from ..memory.ogmem_memory_manager import OGMemoryMemoryManager

            mm = getattr(agent, "memory_manager", None)
            if isinstance(mm, OGMemoryMemoryManager):
                self.ogmem_mm = mm
                logger.info(
                    "[ogmem-ctx] cached memory_manager, session=%s",
                    mm._session_id,
                )
            else:
                logger.warning(
                    "[ogmem-ctx] agent.memory_manager is %s, "
                    "expected OGMemoryMemoryManager",
                    type(mm).__name__ if mm else "None",
                )
        return self.ogmem_mm

    def _new_messages(self, agent: Any) -> list[Msg]:
        """Return messages added since last after_turn call."""
        memory = agent.memory
        msgs = [msg for msg, _ in memory.content]
        new = msgs[self._last_sent_idx:]
        self._last_sent_idx = len(msgs)
        return new

    # ------------------------------------------------------------------
    # Lifecycle: start / close
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """No-op. mm owns bootstrap."""

    async def close(self) -> bool:
        """No-op. mm owns dispose."""
        self.ogmem_mm = None
        return True

    # ------------------------------------------------------------------
    # Agent lifecycle hooks
    # ------------------------------------------------------------------

    async def pre_reply(
        self,
        agent: Any,
        kwargs: dict[str, Any],
    ) -> dict[str, Any] | None:
        """No-op."""
        return None

    async def pre_reasoning(
        self,
        agent: Any,
        kwargs: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Retrieve relevant memories before each reasoning step."""
        msg = kwargs.get("msg")
        if msg is None:
            return None
        try:
            return await self._ogmem_mm(agent).retrieve(
                msg, agent_name=agent.name,
            )
        except Exception:
            logger.exception("[ogmem-ctx] pre_reasoning failed")
            return None

    async def post_acting(
        self,
        agent: Any,
        kwargs: dict[str, Any],
        output: Any,
    ) -> Msg | None:
        """Persist tool interaction to oG-Memory after each acting step."""
        new_msgs = self._new_messages(agent)
        if not new_msgs:
            return None
        try:
            await self._ogmem_mm(agent).after_turn(new_msgs)
        except Exception:
            logger.exception("[ogmem-ctx] post_acting failed")
        return None

    async def post_reply(
        self,
        agent: Any,
        kwargs: dict[str, Any],
        output: Any,
    ) -> Msg | None:
        """Persist final reply to oG-Memory."""
        new_msgs = self._new_messages(agent)
        if not new_msgs:
            return None
        try:
            await self._ogmem_mm(agent).after_turn(new_msgs)
        except Exception:
            logger.exception("[ogmem-ctx] post_reply failed")
        return None

    # ------------------------------------------------------------------
    # Agent context
    # ------------------------------------------------------------------

    def get_agent_context(self, **kwargs) -> AgentContext:
        """Create local AgentContext with token counting support."""
        agent_config = load_agent_config(self.agent_id)
        return AgentContext(
            token_counter=get_token_counter(agent_config),
        )

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    async def compact_context(
        self,
        messages: list[Msg],
        previous_summary: str = "",
        extra_instruction: str = "",
    ) -> dict:
        """Delegate to ogmem_mm.compact() for server-side compaction."""
        if self.ogmem_mm is None:
            return {
                "success": False,
                "reason": "memory_manager not initialized",
                "history_compact": "",
                "before_tokens": 0,
                "after_tokens": 0,
            }
        try:
            return await self.ogmem_mm.compact()
        except Exception:
            logger.exception("[ogmem-ctx] compact_context failed")
            return {
                "success": False,
                "reason": "compact failed",
                "history_compact": "",
                "before_tokens": 0,
                "after_tokens": 0,
            }
