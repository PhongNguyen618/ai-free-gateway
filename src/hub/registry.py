"""
Plug-and-Play Project & MCP-Lite Tool Registry for AI Free Gateway.
Enables automatic discovery, heartbeat tracking, and bidirectional command dispatch
across connected agent projects without requiring direct inbound ports on clients.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_free_gateway.hub.registry")


class ProjectRegistry:
    def __init__(self, heartbeat_timeout_seconds: float = 60.0):
        self.heartbeat_timeout = max(0.1, float(heartbeat_timeout_seconds))
        self._projects: Dict[str, Dict[str, Any]] = {}
        # project_id -> list of pending command dicts
        self._command_queues: Dict[str, List[Dict[str, Any]]] = {}
        # command_id -> asyncio.Future
        self._pending_futures: Dict[str, asyncio.Future] = {}
        # command_id -> metadata
        self._commands_metadata: Dict[str, Dict[str, Any]] = {}

    def register_project(
        self,
        project_id: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Registers or re-announces a project and its available tools."""
        clean_id = (project_id or "default").strip()
        now = time.time()

        normalized_tools = []
        for t in tools or []:
            if isinstance(t, dict) and "name" in t:
                normalized_tools.append({
                    "name": str(t["name"]).strip(),
                    "description": str(t.get("description", "")).strip(),
                    "parameters": t.get("parameters", {}),
                    "project_id": clean_id,
                })

        record = {
            "project_id": clean_id,
            "display_name": display_name or clean_id,
            "description": description or f"Agent project '{clean_id}'",
            "tools": normalized_tools,
            "tools_count": len(normalized_tools),
            "status": "online",
            "registered_at": self._projects.get(clean_id, {}).get("registered_at", now),
            "last_heartbeat": now,
        }
        self._projects[clean_id] = record
        if clean_id not in self._command_queues:
            self._command_queues[clean_id] = []

        logger.info(f"Registered project '{clean_id}' with {len(normalized_tools)} tools.")
        return record

    def heartbeat(self, project_id: str) -> List[Dict[str, Any]]:
        """
        Receives heartbeat ping from project node and pops queued commands for execution.
        """
        clean_id = (project_id or "default").strip()
        now = time.time()

        if clean_id in self._projects:
            self._projects[clean_id]["last_heartbeat"] = now
            self._projects[clean_id]["status"] = "online"
        else:
            # Auto-register stub if not pre-announced
            self.register_project(clean_id)

        # Pop pending commands for this project
        queue = self._command_queues.get(clean_id, [])
        commands_to_send = list(queue)
        queue.clear()
        return commands_to_send

    async def dispatch_command(
        self,
        project_id: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Any:
        """
        Dispatches a command to a registered project and awaits its execution result.
        """
        clean_id = project_id.strip()
        self._prune_stale()

        proj = self._projects.get(clean_id)
        if not proj or proj.get("status") != "online":
            raise RuntimeError(f"Project '{clean_id}' is currently offline or not registered.")

        command_id = f"cmd_{secrets.token_hex(8)}"
        command_payload = {
            "command_id": command_id,
            "project_id": clean_id,
            "tool_name": tool_name.strip(),
            "arguments": arguments or {},
            "created_at": time.time(),
        }

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_futures[command_id] = future
        self._commands_metadata[command_id] = command_payload

        # Enqueue command
        if clean_id not in self._command_queues:
            self._command_queues[clean_id] = []
        self._command_queues[clean_id].append(command_payload)

        try:
            return await asyncio.wait_for(future, timeout=max(1.0, float(timeout)))
        except asyncio.TimeoutError:
            self._pending_futures.pop(command_id, None)
            raise TimeoutError(f"Command '{tool_name}' on project '{clean_id}' timed out after {timeout}s.")
        finally:
            self._pending_futures.pop(command_id, None)
            self._commands_metadata.pop(command_id, None)

    def submit_command_result(
        self,
        command_id: str,
        project_id: str,
        status: str,
        result: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Called by project node to deliver the outcome of an executed command."""
        future = self._pending_futures.get(command_id)
        if not future or future.done():
            return False

        if status.lower() == "success":
            future.set_result(result)
        else:
            future.set_exception(RuntimeError(error or f"Command '{command_id}' execution failed on node."))
        return True

    def get_all_projects(self) -> List[Dict[str, Any]]:
        """Returns all registered projects with up-to-date online/offline status."""
        self._prune_stale()
        return list(self._projects.values())

    def get_online_projects(self) -> List[Dict[str, Any]]:
        """Returns only currently online projects."""
        self._prune_stale()
        return [p for p in self._projects.values() if p.get("status") == "online"]

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Aggregates all available tools across all currently online projects."""
        self._prune_stale()
        tools = []
        for p in self._projects.values():
            if p.get("status") == "online":
                tools.extend(p.get("tools", []))
        return tools

    def _prune_stale(self) -> None:
        """Mark projects that missed heartbeat threshold as offline."""
        now = time.time()
        for p in self._projects.values():
            if (now - p.get("last_heartbeat", 0.0)) > self.heartbeat_timeout:
                p["status"] = "offline"

    def clear(self) -> None:
        """Resets all registry state."""
        self._projects.clear()
        self._command_queues.clear()
        for f in self._pending_futures.values():
            if not f.done():
                f.cancel()
        self._pending_futures.clear()
        self._commands_metadata.clear()
