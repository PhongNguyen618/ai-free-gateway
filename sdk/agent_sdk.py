"""Universal AI Agent SDK for ai-free-gateway.

Zero-dependency Python SDK designed to integrate any local or remote project
into the AI Free Gateway ecosystem in under 30 seconds.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("ai_agent_sdk")


def extract_json_payload(text: str) -> Union[Dict[str, Any], List[Any]]:
    """
    Robust JSON extractor from LLM text responses.
    Handles Markdown code blocks, preamble text, trailing commas, and partial wrappers.
    """
    text_clean = text.strip()

    # 1. Match ```json ... ``` or ``` ... ```
    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text_clean, re.IGNORECASE)
    if code_block_match:
        candidate = code_block_match.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            text_clean = candidate

    # 2. Match outer { ... } or [ ... ]
    first_brace = text_clean.find("{")
    first_bracket = text_clean.find("[")

    start_idx = -1
    is_object = True

    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        start_idx = first_brace
        end_idx = text_clean.rfind("}")
        is_object = True
    elif first_bracket != -1:
        start_idx = first_bracket
        end_idx = text_clean.rfind("]")
        is_object = False
    else:
        end_idx = -1

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        raw_json = text_clean[start_idx : end_idx + 1]
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError:
            # Fallback cleanup: remove trailing commas before } or ]
            cleaned = re.sub(r",\s*([\]}])", r"\1", raw_json)
            return json.loads(cleaned)

    # 3. Direct attempt
    return json.loads(text_clean)


class UniversalAIClient:
    """
    Universal Client for AI Free Gateway (OpenAI-compatible).
    No external pip requirements (uses standard library urllib).
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000/v1",
        api_key: str = "sk-local-dev-token",
        project_id: str = "default",
        priority: str = "normal",
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "sk-local-dev-token"
        self.project_id = project_id
        self.priority = priority.lower()
        self.timeout = timeout

    def _post_chat(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-Project-ID": self.project_id,
            "X-Project-Priority": self.priority,
            "User-Agent": "UniversalAIAgentSDK/1.0",
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                body = resp.read().decode("utf-8")
                return json.loads(body), resp_headers
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gateway HTTP Error {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Gateway Connection Error: {e.reason}") from e

    def ask_text(
        self,
        prompt: str,
        model: str = "auto",
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> str:
        """
        Sends a simple completion prompt and returns the assistant's text response.
        """
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        payload.update(kwargs)

        res_json, _ = self._post_chat(payload)
        choices = res_json.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "").strip()

    def ask_json(
        self,
        prompt: str,
        schema_hint: Optional[str] = None,
        model: str = "auto",
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_retries: int = 2,
        **kwargs: Any,
    ) -> Union[Dict[str, Any], List[Any]]:
        """
        Sends a prompt requesting a JSON structure and guarantees a valid parsed JSON output.
        Automatically retries with targeted error feedback if invalid.
        """
        sys_directive = "You MUST reply ONLY with valid JSON. Do not include introductory text, explanations, or chatter."
        if schema_hint:
            sys_directive += f"\nExpected schema:\n{schema_hint}"
        if system:
            combined_system = f"{system}\n\n{sys_directive}"
        else:
            combined_system = sys_directive

        current_prompt = prompt
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            raw_text = self.ask_text(
                prompt=current_prompt,
                model=model,
                system=combined_system,
                temperature=temperature,
                **kwargs,
            )
            try:
                parsed = extract_json_payload(raw_text)
                return parsed
            except Exception as e:
                last_error = e
                logger.warning(f"ask_json parse attempt {attempt+1} failed: {e}. Raw: {raw_text[:200]}")
                current_prompt = (
                    f"{prompt}\n\n[ERROR: Your previous output was not valid JSON: {str(e)}]\n"
                    f"Please output strictly valid JSON now."
                )

        raise ValueError(f"Failed to obtain valid JSON after {max_retries + 1} attempts: {last_error}")


class BatchAIAgent:
    """
    Groups individual processing items into single-prompt batches to save 50-80% of Gateway RPM.
    Ideal for scrapers, data tagging, sentiment analysis, transaction classification.
    """

    def __init__(
        self,
        client: UniversalAIClient,
        default_batch_size: int = 5,
        default_model: str = "fast",
    ):
        self.client = client
        self.default_batch_size = max(1, default_batch_size)
        self.default_model = default_model

    def process_batch(
        self,
        items: List[Any],
        task_instruction: str,
        batch_size: Optional[int] = None,
        model: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> List[Any]:
        """
        Splits items into batches, executes batch inference via Gateway, and aggregates results.
        Returns a 1-to-1 matching list of results.
        """
        if not items:
            return []

        bs = batch_size or self.default_batch_size
        selected_model = model or self.default_model
        results: List[Any] = []

        old_priority = self.client.priority
        if priority:
            self.client.priority = priority

        try:
            for i in range(0, len(items), bs):
                chunk = items[i : i + bs]
                indexed_chunk = [{"index": idx, "data": it} for idx, it in enumerate(chunk)]

                prompt = (
                    f"Task Instruction: {task_instruction}\n\n"
                    f"Process the following {len(chunk)} items in batch:\n"
                    f"{json.dumps(indexed_chunk, ensure_ascii=False, indent=2)}\n\n"
                    "Output a strictly valid JSON array of objects, one for each item in the EXACT same order.\n"
                    "Format: [{\"index\": 0, \"result\": ...}, {\"index\": 1, \"result\": ...}]"
                )

                batch_res = self.client.ask_json(
                    prompt=prompt,
                    model=selected_model,
                    temperature=0.2,
                    schema_hint="[{\"index\": int, \"result\": any}]",
                )

                if isinstance(batch_res, list):
                    # Sort by index to guarantee order
                    sorted_items = sorted(batch_res, key=lambda x: x.get("index", 0) if isinstance(x, dict) else 0)
                    for item_out in sorted_items:
                        if isinstance(item_out, dict) and "result" in item_out:
                            results.append(item_out["result"])
                        else:
                            results.append(item_out)
                else:
                    results.extend([batch_res] * len(chunk))

            return results
        finally:
            self.client.priority = old_priority


class ReActToolAgent:
    """
    Multi-step Autonomous Reasoning and Action (ReAct) agent.
    Runs client-side tool loops with built-in loop detection and max-step safety guards.
    """

    def __init__(
        self,
        client: UniversalAIClient,
        tools: Dict[str, Callable[[str], str]],
        model: str = "reasoning",
        max_steps: int = 5,
        system_persona: Optional[str] = None,
    ):
        self.client = client
        self.tools = tools
        self.model = model
        self.max_steps = max_steps
        self.system_persona = system_persona or "You are an expert autonomous ReAct problem solver."

    def _render_tool_descriptions(self) -> str:
        lines = []
        for name, func in self.tools.items():
            doc = func.__doc__ or "No description provided."
            doc_first_line = doc.strip().split("\n")[0]
            lines.append(f"- {name}: {doc_first_line}")
        return "\n".join(lines)

    def run(self, goal: str) -> Dict[str, Any]:
        """
        Executes autonomous ReAct loop until 'Final Answer:' is found or max_steps reached.
        """
        tool_desc = self._render_tool_descriptions()
        system_prompt = (
            f"{self.system_persona}\n\n"
            "You have access to the following tools:\n"
            f"{tool_desc}\n\n"
            "Use the following format strictly:\n"
            "Thought: consider what action to take next\n"
            "Action: tool_name[tool_input]\n"
            "Observation: result of the tool\n"
            "... (repeat Thought/Action/Observation as needed)\n"
            "Thought: I now know the final answer\n"
            "Final Answer: your definitive conclusion to the goal\n\n"
            "IMPORTANT: If you already have all information to answer, state 'Final Answer:' immediately."
        )

        history: List[str] = [f"Goal: {goal}"]
        action_history: List[Tuple[str, str]] = []
        steps_taken = 0

        final_answer = ""

        while steps_taken < self.max_steps:
            steps_taken += 1
            conversation = "\n\n".join(history)

            step_output = self.client.ask_text(
                prompt=conversation,
                model=self.model,
                system=system_prompt,
                temperature=0.2,
                max_tokens=512,
            )
            history.append(step_output)

            # Check for Final Answer
            final_match = re.search(r"Final Answer:\s*(.*)", step_output, re.IGNORECASE | re.DOTALL)
            if final_match:
                final_answer = final_match.group(1).strip()
                break

            # Parse Action
            action_match = re.search(r"Action:\s*([a-zA-Z0-9_\-]+)\[(.*?)\]", step_output, re.DOTALL)
            if not action_match:
                # Secondary format: Action: name \n Action Input: input
                action_match_alt = re.search(r"Action:\s*([a-zA-Z0-9_\-]+)\s*\nAction Input:\s*(.*)", step_output)
                if action_match_alt:
                    tool_name = action_match_alt.group(1).strip()
                    tool_arg = action_match_alt.group(2).strip()
                else:
                    # No tool action found, nudge agent
                    history.append("Observation: Error: No valid 'Action: tool[input]' or 'Final Answer:' detected. Please choose a tool or provide Final Answer.")
                    continue
            else:
                tool_name = action_match.group(1).strip()
                tool_arg = action_match.group(2).strip()

            # Client-side Loop Detection Guard
            action_sig = (tool_name, tool_arg)
            if action_history.count(action_sig) >= 2:
                obs = f"Observation: LoopGuard triggered: You have already executed '{tool_name}' with arguments '{tool_arg}' multiple times without progress. Please formulate your Final Answer or choose a different action."
                history.append(obs)
                action_history.append(action_sig)
                continue

            action_history.append(action_sig)

            # Execute tool
            if tool_name not in self.tools:
                obs = f"Observation: Error: Tool '{tool_name}' is not in available tools [{', '.join(self.tools.keys())}]."
            else:
                try:
                    tool_res = self.tools[tool_name](tool_arg)
                    obs = f"Observation: {tool_res}"
                except Exception as e:
                    obs = f"Observation: Tool Execution Error: {str(e)}"

            history.append(obs)

        if not final_answer:
            # Fallback final answer extraction from last output
            final_answer = step_output

        return {
            "goal": goal,
            "steps_count": steps_taken,
            "final_answer": final_answer,
            "history": history,
            "actions_executed": action_history,
        }
