"""
AI Auto-Orchestrator for All-in-One Agent Hub.
Receives natural language instructions in Vietnamese or English, analyzes available
MCP-lite tools across all online projects, executes multi-step tool chaining (S03),
enforces client-side loop guards, and synthesizes unified answers.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

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
        max_steps: int = 3,
    ) -> Dict[str, Any]:
        """
        Takes a natural language request, plans and executes multi-step reasoning
        across registered project tools, and returns the final synthesized conclusion.
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

        # Tools catalog representation
        tools_summary = []
        for t in tools:
            tools_summary.append({
                "project_id": t.get("project_id"),
                "tool_name": t.get("name"),
                "description": t.get("description"),
                "parameters": t.get("parameters", {}),
            })

        # S03: Multi-step chaining execution
        steps_executed: List[Dict[str, Any]] = []
        seen_calls: Set[Tuple[str, str, str]] = set()
        final_answer = ""
        last_action = "direct_answer"
        last_tool_called = ""
        last_tool_success = False
        last_tool_output: Any = None

        system_prompt = (
            "Bạn là AI Trưởng Phòng Điều Phối (Master Orchestrator). Nhiệm vụ của bạn là xem câu hỏi của người dùng và chọn công cụ phù hợp từ các dự án đang Online.\n\n"
            f"Danh sách công cụ hiện có:\n{json.dumps(tools_summary, ensure_ascii=False, indent=2)}\n\n"
            "Hãy phân tích và trả về DUY NHẤT một chuỗi JSON theo 1 trong các định dạng sau:\n"
            "1. Nếu cần gọi công cụ:\n"
            "{\n"
            '  "action": "call_tool",\n'
            '  "project_id": "tên_project",\n'
            '  "tool_name": "tên_công_cụ",\n'
            '  "arguments": {"tham_số": "giá_trị"}\n'
            "}\n"
            "2. Nếu đã có đủ dữ liệu hoặc là câu hỏi thông thường:\n"
            "{\n"
            '  "action": "finish",\n'
            '  "answer": "câu trả lời hoàn chỉnh bằng tiếng Việt"\n'
            "}\n"
            "3. Nếu cần phát thông báo lên Bảng tin chung:\n"
            "{\n"
            '  "action": "publish_event",\n'
            '  "topic": "chủ_đề",\n'
            '  "payload": {"nội_dung": "..."}\n'
            "}"
        )

        for step_idx in range(max_steps):
            history_repr = ""
            if steps_executed:
                history_repr = "\n\nCác bước đã thực thi trước đó:\n" + json.dumps(steps_executed, ensure_ascii=False, indent=2)

            plan_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Yêu cầu người dùng: {user_query}{history_repr}\n\nHãy quyết định bước tiếp theo:"},
            ]

            raw_plan = await self._call_ai(plan_messages, model_tier)
            try:
                plan = extract_json_payload(raw_plan)
            except Exception:
                plan = {"action": "finish", "answer": raw_plan}

            action = str(plan.get("action", "finish")).lower()
            last_action = action

            if action == "call_tool":
                proj_id = plan.get("project_id", "")
                t_name = plan.get("tool_name", "")
                t_args = plan.get("arguments", {})
                last_tool_called = f"{proj_id}:{t_name}"

                # S03: Loop Guard
                arg_sig = json.dumps(t_args, sort_keys=True, ensure_ascii=False)
                call_sig = (proj_id, t_name, arg_sig)
                if call_sig in seen_calls:
                    logger.warning(f"Loop guard triggered on duplicate tool call: {call_sig}")
                    steps_executed.append({
                        "step": step_idx + 1,
                        "action": "call_tool",
                        "tool": last_tool_called,
                        "arguments": t_args,
                        "status": "loop_detected",
                        "output": "LoopGuard: Công cụ này vừa được gọi với cùng tham số. Hãy tổng hợp câu trả lời ngay.",
                    })
                    break

                seen_calls.add(call_sig)

                # Dispatch command
                try:
                    tool_output = await self.registry.dispatch_command(
                        project_id=proj_id,
                        tool_name=t_name,
                        arguments=t_args,
                        timeout=35.0,
                    )
                    last_tool_success = True
                except Exception as exc:
                    tool_output = f"Lỗi thực thi công cụ: {str(exc)}"
                    last_tool_success = False

                last_tool_output = tool_output
                steps_executed.append({
                    "step": step_idx + 1,
                    "action": "call_tool",
                    "tool": last_tool_called,
                    "arguments": t_args,
                    "status": "success" if last_tool_success else "error",
                    "output": tool_output,
                })

            elif action == "publish_event":
                evt_topic = plan.get("topic", "general.announcement")
                evt_payload = plan.get("payload", {})
                evt = self.event_bus.publish(
                    topic=evt_topic,
                    source_project="orchestrator",
                    payload=evt_payload,
                )
                steps_executed.append({
                    "step": step_idx + 1,
                    "action": "publish_event",
                    "topic": evt_topic,
                    "payload": evt_payload,
                })
                final_answer = f"Đã phát thông điệp lên Bảng tin chung ở chủ đề '{evt_topic}'."
                break

            else:
                # "finish" or "direct_answer"
                final_answer = plan.get("answer", raw_plan)
                break

        # Synthesis if multiple steps were run and no final answer was formulated
        if not final_answer:
            synth_prompt = (
                "Bạn là AI Trưởng Phòng Điều Phối. Hãy tổng hợp toàn bộ kết quả từ các bước thực thi thành câu trả lời hoàn chỉnh, rõ ràng, thân thiện bằng tiếng Việt cho người dùng.\n\n"
                f"Yêu cầu gốc: {user_query}\n"
                f"Nhật ký các bước: {json.dumps(steps_executed, ensure_ascii=False, indent=2)}"
            )
            synth_messages = [{"role": "user", "content": synth_prompt}]
            final_answer = await self._call_ai(synth_messages, model_tier)

        has_tool_call = any(s.get("action") == "call_tool" for s in steps_executed)
        has_event_pub = any(s.get("action") == "publish_event" for s in steps_executed)

        if has_tool_call:
            normalized_action = "call_tool"
        elif has_event_pub:
            normalized_action = "publish_event"
        else:
            normalized_action = "direct_answer"

        return {
            "query": user_query,
            "plan_action": normalized_action,
            "steps_count": len(steps_executed),
            "steps_executed": steps_executed,
            "tool_called": last_tool_called,
            "tool_success": last_tool_success,
            "tool_output": last_tool_output,
            "final_answer": final_answer,
            "online_projects_count": len(online_projects),
        }
