"""
AI Auto-Orchestrator for All-in-One Agent Hub.
Receives natural language instructions in Vietnamese or English, analyzes available
MCP-lite tools across all online projects, dispatches tasks, and synthesizes answers.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

from sdk.agent_sdk import extract_json_payload
from src.hub.event_bus import SharedEventBus
from src.hub.registry import ProjectRegistry

logger = logging.getLogger("ai_free_gateway.hub.orchestrator")


class AIAutoOrchestrator:
    def __init__(
        self,
        registry: ProjectRegistry,
        event_bus: SharedEventBus,
        chat_fn: Optional[Callable[[List[Dict[str, str]], str], Coroutine[Any, Any, str]]] = None,
    ):
        self.registry = registry
        self.event_bus = event_bus
        self.chat_fn = chat_fn

    async def _call_ai(self, messages: List[Dict[str, str]], model_tier: str = "reasoning") -> str:
        """Invokes internal AI reasoning capability."""
        if not self.chat_fn:
            raise RuntimeError("AI chat function is not configured on Orchestrator.")
        return await self.chat_fn(messages, model_tier)

    async def orchestrate(
        self,
        user_query: str,
        model_tier: str = "reasoning",
    ) -> Dict[str, Any]:
        """
        Takes a natural language request, plans execution across registered tools,
        dispatches to project nodes, and returns the unified answer.
        """
        tools = self.registry.get_all_tools()
        online_projects = self.registry.get_online_projects()

        if not tools:
            # No tools available, ask AI directly
            messages = [
                {
                    "role": "system",
                    "content": "Bạn là AI Điều Phối Trung Tâm của hệ thống Agent. Hiện tại chưa có dự án con nào cắm vào. Hãy trả lời câu hỏi của người dùng một cách hữu ích bằng tiếng Việt.",
                },
                {"role": "user", "content": user_query},
            ]
            try:
                answer = await self._call_ai(messages, model_tier)
            except Exception as e:
                answer = f"Hiện tại không có dự án nào đang cắm vào hệ thống, và AI gặp lỗi: {e}"

            return {
                "query": user_query,
                "plan_action": "direct_answer",
                "tools_used": [],
                "final_answer": answer,
                "online_projects_count": len(online_projects),
            }

        # Step 1: Planning with AI
        tools_summary = []
        for t in tools:
            tools_summary.append({
                "project_id": t.get("project_id"),
                "tool_name": t.get("name"),
                "description": t.get("description"),
                "parameters": t.get("parameters", {}),
            })

        planning_prompt = (
            "Bạn là AI Trưởng Phòng Điều Phối (Master Orchestrator). Nhiệm vụ của bạn là xem câu hỏi của người dùng và chọn công cụ phù hợp từ các dự án đang Online.\n\n"
            f"Danh sách công cụ hiện có:\n{json.dumps(tools_summary, ensure_ascii=False, indent=2)}\n\n"
            "Hãy phân tích và trả về DUY NHẤT một chuỗi JSON theo định dạng sau:\n"
            "1. Nếu cần gọi công cụ:\n"
            "{\n"
            '  "action": "call_tool",\n'
            '  "project_id": "tên_project",\n'
            '  "tool_name": "tên_công_cụ",\n'
            '  "arguments": {"tham_số": "giá_trị"}\n'
            "}\n"
            "2. Nếu là câu hỏi thông thường không cần công cụ:\n"
            "{\n"
            '  "action": "direct_answer",\n'
            '  "answer": "câu trả lời bằng tiếng Việt"\n'
            "}\n"
            "3. Nếu cần phát thông báo lên Bảng tin chung:\n"
            "{\n"
            '  "action": "publish_event",\n'
            '  "topic": "chủ_đề",\n'
            '  "payload": {"nội_dung": "..."}\n'
            "}"
        )

        plan_messages = [
            {"role": "system", "content": planning_prompt},
            {"role": "user", "content": f"Yêu cầu: {user_query}"},
        ]

        raw_plan = await self._call_ai(plan_messages, model_tier)
        try:
            plan = extract_json_payload(raw_plan)
        except Exception:
            plan = {"action": "direct_answer", "answer": raw_plan}

        action = str(plan.get("action", "direct_answer")).lower()

        # Step 2: Execution
        if action == "call_tool":
            proj_id = plan.get("project_id", "")
            t_name = plan.get("tool_name", "")
            t_args = plan.get("arguments", {})

            try:
                # Dispatch command to connected project node
                tool_output = await self.registry.dispatch_command(
                    project_id=proj_id,
                    tool_name=t_name,
                    arguments=t_args,
                    timeout=35.0,
                )
                success = True
            except Exception as exc:
                tool_output = f"Lỗi thực thi công cụ: {str(exc)}"
                success = False

            # Step 3: Synthesis
            synth_prompt = (
                "Bạn là AI Trưởng Phòng Điều Phối. Hãy tổng hợp kết quả nhận được từ dự án con thành câu trả lời hoàn chỉnh, rõ ràng, thân thiện bằng tiếng Việt cho người dùng.\n\n"
                f"Yêu cầu gốc: {user_query}\n"
                f"Công cụ đã gọi: {proj_id} -> {t_name}\n"
                f"Kết quả công cụ: {json.dumps(tool_output, ensure_ascii=False) if isinstance(tool_output, (dict, list)) else str(tool_output)}"
            )
            synth_messages = [{"role": "user", "content": synth_prompt}]
            final_answer = await self._call_ai(synth_messages, model_tier)

            return {
                "query": user_query,
                "plan_action": "call_tool",
                "tool_called": f"{proj_id}:{t_name}",
                "tool_success": success,
                "tool_output": tool_output,
                "final_answer": final_answer,
                "online_projects_count": len(online_projects),
            }

        elif action == "publish_event":
            evt_topic = plan.get("topic", "general.announcement")
            evt_payload = plan.get("payload", {})
            evt = self.event_bus.publish(
                topic=evt_topic,
                source_project="orchestrator",
                payload=evt_payload,
            )
            return {
                "query": user_query,
                "plan_action": "publish_event",
                "published_event": evt,
                "final_answer": f"Đã phát thông điệp lên Bảng tin chung ở chủ đề '{evt_topic}'.",
                "online_projects_count": len(online_projects),
            }

        else:
            return {
                "query": user_query,
                "plan_action": "direct_answer",
                "tools_used": [],
                "final_answer": plan.get("answer", raw_plan),
                "online_projects_count": len(online_projects),
            }
