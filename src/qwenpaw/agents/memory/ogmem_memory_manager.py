# -*- coding: utf-8 -*-
"""oG-Memory HTTP adapter for QwenPaw memory manager.

Owns session_id and HTTP client — the sole point of contact with the
oG-Memory HTTP service.  OGMemoryContextManager delegates here for all
ogmem interactions.

Public API used by context_manager:
  after_turn(messages) → POST /after_turn
  compact()           → POST /compact
  retrieve(messages)  → POST /compose (via memory_search)
"""
import json
import logging
import os
import uuid
from collections.abc import Callable

import httpx
from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolUseBlock
from agentscope.tool import ToolResponse

from .base_memory_manager import BaseMemoryManager, memory_registry

logger = logging.getLogger(__name__)

OGMEM_MEMORY_GUIDANCE_ZH = """\
## 记忆

你可以使用 `memory_search` 工具搜索长期记忆。在回答关于过往工作、决策、偏好、
待办的问题前，先用 `memory_search` 检索相关记忆。

重要信息会自动从对话中提取并持久化，无需手动保存。"""

OGMEM_MEMORY_GUIDANCE_EN = """\
## Memory

You can use the `memory_search` tool to search long-term memory. Before answering
questions about past work, decisions, preferences, or todos, use `memory_search`
to retrieve relevant memories.

Important information is automatically extracted from conversations and persisted.
No manual saving needed."""


def _msg_to_dict(msg: Msg) -> dict:
    """Convert an AgentScope Msg to a {role, content} dict for oG-Memory."""
    content = msg.content
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    text_parts.append(
                        f"[tool: {block.get('name')}] "
                        f"{json.dumps(block.get('input', {}), ensure_ascii=False)}",
                    )
                elif block.get("type") == "tool_result":
                    outputs = block.get("output", [])
                    if isinstance(outputs, list):
                        for o in outputs:
                            if isinstance(o, dict) and o.get("type") == "text":
                                text_parts.append(o.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        content = "\n".join(text_parts)
    return {"role": msg.role, "content": content or ""}


@memory_registry.register("ogmem")
class OGMemoryMemoryManager(BaseMemoryManager):
    """Memory manager backed by oG-Memory HTTP service.

    Delegates lifecycle, search, and persistence to an oG-Memory instance
    via its REST API.  Owns the session_id and HTTP client.
    """

    def __init__(self, working_dir: str, agent_id: str):
        super().__init__(working_dir=working_dir, agent_id=agent_id)
        self._base_url = os.environ.get(
            "OGMEMORY_URL", "http://localhost:8090",
        )
        self._account_id = os.environ.get("OGMEMORY_ACCOUNT_ID", "default")
        self._user_id = os.environ.get("OGMEMORY_USER_ID", agent_id)
        self._session_id = uuid.uuid4().hex
        self._http: httpx.AsyncClient | None = None
        logger.info(
            "[ogmem] init: agent_id=%s, base_url=%s, session=%s",
            agent_id, self._base_url, self._session_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_params(self) -> dict:
        return {
            "sessionId": self._session_id,
            "accountId": self._account_id,
            "userId": self._user_id,
            "agentId": self.agent_id,
        }

    # ------------------------------------------------------------------
    # POST helpers — one per oG-Memory API
    # ------------------------------------------------------------------

    async def _post_bootstrap(self) -> dict:
        """POST /api/v1/bootstrap"""
        params = self._base_params()
        logger.info("[ogmem] POST /api/v1/bootstrap params=%s", params)
        resp = await self._http.post("/api/v1/bootstrap", json=params)
        logger.info(
            "[ogmem] POST /api/v1/bootstrap => status=%s body=%s",
            resp.status_code, resp.text[:300],
        )
        resp.raise_for_status()
        return resp.json()

    async def _post_dispose(self) -> None:
        """POST /api/v1/dispose"""
        params = self._base_params()
        logger.info("[ogmem] POST /api/v1/dispose params=%s", params)
        try:
            resp = await self._http.post("/api/v1/dispose", json=params)
            logger.info(
                "[ogmem] POST /api/v1/dispose => status=%s body=%s",
                resp.status_code, resp.text[:300],
            )
        except Exception:
            logger.exception("[ogmem] dispose request failed")

    async def _post_after_turn(self, msg_dicts: list[dict]) -> dict:
        """POST /api/v1/after_turn — accumulate messages, maybe extract."""
        params = {**self._base_params(), "messages": msg_dicts}
        log_params = {k: v for k, v in params.items() if k != "messages"}
        log_params["message_count"] = len(msg_dicts)
        logger.info("[ogmem] POST /api/v1/after_turn params=%s", log_params)
        resp = await self._http.post("/api/v1/after_turn", json=params)
        logger.info(
            "[ogmem] POST /api/v1/after_turn => status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        resp.raise_for_status()
        return resp.json()

    async def _post_compact(self, token_budget: int = 128_000) -> dict:
        """POST /api/v1/compact — force extract + archive, return summary."""
        params = {**self._base_params(), "tokenBudget": token_budget}
        logger.info("[ogmem] POST /api/v1/compact params=%s", params)
        resp = await self._http.post("/api/v1/compact", json=params)
        logger.info(
            "[ogmem] POST /api/v1/compact => status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        resp.raise_for_status()
        return resp.json()

    async def _post_compose(self, query: str) -> dict:
        """POST /api/v1/compose — search memories, assemble context."""
        params = {**self._base_params(), "prompt": query, "messages": []}
        logger.info("[ogmem] POST /api/v1/compose query=%r", query)
        resp = await self._http.post("/api/v1/compose", json=params)
        logger.info(
            "[ogmem] POST /api/v1/compose => status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Public methods for context_manager delegation
    # ------------------------------------------------------------------

    async def after_turn(self, messages: list[Msg]) -> str:
        """Persist messages to oG-Memory buffer.

        Called by OGMemoryContextManager.post_acting() and post_reply().
        Accumulates messages in server-side buffer; extraction is
        threshold-based.
        """
        if not self._http:
            return ""
        dicts = [_msg_to_dict(m) for m in messages]
        data = await self._post_after_turn(dicts)
        return (
            f"oG-Memory: extracted={data.get('candidates_extracted', 0)}, "
            f"written={data.get('writes_completed', 0)}"
        )

    async def compact(self) -> dict:
        """Force compact oG-Memory buffer. Returns standard compact result.

        Called by OGMemoryContextManager.compact_context().
        If buffer is empty (already extracted by after_turn), returns
        success=False.
        """
        if not self._http:
            return {
                "success": False, "reason": "not connected",
                "history_compact": "", "before_tokens": 0,
                "after_tokens": 0,
            }
        data = await self._post_compact()
        if not data.get("ok") or not data.get("compacted"):
            return {
                "success": False,
                "reason": data.get("reason", "not compacted"),
                "history_compact": "", "before_tokens": 0,
                "after_tokens": 0,
            }
        result = data.get("result", {})
        summary = result.get("summary", "")
        return {
            "success": bool(summary),
            "reason": "" if summary else "empty summary",
            "history_compact": summary,
            "before_tokens": result.get("tokensBefore", 0),
            "after_tokens": result.get("tokensAfter", 0),
        }

    # ------------------------------------------------------------------
    # BaseMemoryManager interface — abstract methods
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create HTTP client, health check, bootstrap session."""
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
        resp = await self._http.get("/api/v1/health")
        logger.info(
            "[ogmem] GET /api/v1/health => status=%s body=%s",
            resp.status_code, resp.text[:200],
        )
        resp.raise_for_status()
        if resp.json().get("status") != "ok":
            raise RuntimeError(f"oG-Memory unhealthy: {resp.json()}")

        await self._post_bootstrap()
        logger.info("[ogmem] session started: %s", self._session_id)

    async def close(self) -> bool:
        """Dispose session and close HTTP client."""
        if self._http:
            await self._post_dispose()
            await self._http.aclose()
            self._http = None
        return True

    def get_memory_prompt(self, language: str = "zh") -> str:
        prompts = {"zh": OGMEM_MEMORY_GUIDANCE_ZH, "en": OGMEM_MEMORY_GUIDANCE_EN}
        return prompts.get(language, OGMEM_MEMORY_GUIDANCE_EN)

    def list_memory_tools(self) -> list[Callable[..., ToolResponse]]:
        return [self.memory_search]

    # ------------------------------------------------------------------
    # BaseMemoryManager interface — optional overrides
    # ------------------------------------------------------------------

    async def summarize(self, messages: list[Msg], **kwargs) -> str:
        """oG-Memory has no native 'summarize' API. We use:
        1. after_turn — accumulate messages + threshold-based extraction
        2. compact — force extract if after_turn didn't trigger

        Note: if after_turn already triggered extraction and cleared the
        buffer, compact will find nothing. This is expected and not an
        error.
        """
        if not self._http:
            return ""
        dicts = [_msg_to_dict(m) for m in messages]
        await self._post_after_turn(dicts)
        compact_data = await self._post_compact()
        summary = compact_data.get("result", {}).get("summary", "")
        return summary

    async def retrieve(
        self,
        messages: list[Msg] | Msg,
        agent_name: str = "",
        **kwargs,
    ) -> dict | None:
        """Retrieve relevant memory and return updated kwargs dict."""
        if not self._http:
            return None

        msgs: list[Msg] = (
            [messages] if isinstance(messages, Msg) else list(messages)
        )

        # Build query from newest messages
        query_parts: list[str] = []
        total = 0
        for msg in reversed(msgs):
            text = (msg.get_text_content() or "").strip()
            if not text:
                continue
            remaining = 100 - total
            if remaining <= 0:
                break
            chunk = text[:remaining]
            query_parts.insert(0, chunk)
            total += len(chunk)

        query = " ".join(query_parts).strip()
        if not query:
            return None

        try:
            result = await self.memory_search(query=query)
            text_content = "\n".join(
                b.get("text", "")
                for b in result.content
                if isinstance(b, dict) and b.get("text")
            )
            if not text_content:
                return None

            _id = uuid.uuid4().hex
            tool_input = {"query": query}
            assistant_msg = Msg(
                name=agent_name,
                role="assistant",
                content=[
                    TextBlock(type="text", text="Searching memory..."),
                    ToolUseBlock(
                        type="tool_use",
                        id=_id,
                        name="memory_search",
                        input=tool_input,
                        raw_input=json.dumps(tool_input, ensure_ascii=False),
                    ),
                ],
            )
            tool_result_msg = Msg(
                name=agent_name,
                role="system",
                content=[
                    ToolResultBlock(
                        type="tool_result",
                        id=_id,
                        name="memory_search",
                        output=[TextBlock(type="text", text=text_content)],
                    ),
                ],
            )
            return {"msg": msgs + [assistant_msg, tool_result_msg]}
        except Exception:
            logger.exception("[ogmem] retrieve failed")
            return None

    # ------------------------------------------------------------------
    # Tool method — registered with agent toolkit
    # ------------------------------------------------------------------

    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
    ) -> ToolResponse:
        """Search memories semantically via oG-Memory.

        Args:
            query: The semantic search query.
            max_results: Maximum results (unused by oG-Memory).
            min_score: Minimum similarity (unused by oG-Memory).

        Returns:
            ToolResponse with search results.
        """
        if not self._http:
            return ToolResponse(
                content=[TextBlock(type="text", text="oG-Memory not started")],
            )
        try:
            data = await self._post_compose(query)
            parts = [
                data[f].strip()
                for f in (
                    "identityContext", "retrievedEvidence",
                    "episodicContext", "sessionContext",
                )
                if data.get(f, "").strip()
            ]
            text = "\n\n---\n\n".join(parts) or "未找到相关记忆。"
            return ToolResponse(
                content=[TextBlock(type="text", text=text)],
            )
        except Exception:
            logger.exception("[ogmem] memory_search failed")
            return ToolResponse(
                content=[TextBlock(type="text", text="记忆搜索失败。")],
            )
