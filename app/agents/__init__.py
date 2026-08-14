"""Production harness components for autonomous agent nodes.

The outer JSON-defined LangGraph continues to own workflow orchestration.  This
package contains the bounded, policy-aware harness used inside the existing
``Autonomous ReAct Agent`` node.
"""

from app.agents.contracts import AgentNodeSpec, AgentResult, ToolResult

__all__ = ["AgentNodeSpec", "AgentResult", "ToolResult"]
