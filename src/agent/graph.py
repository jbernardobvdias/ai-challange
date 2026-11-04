"""LangGraph agent graph.

    START -> input_guard -> agent <-> tools
                 |            |
              (blocked)       v
                 |       output_guard
                 v            |
                END <---------+

Confirmation and fallback nodes from docs/architecture.md §3 are added in later steps.
"""
import logging
from collections.abc import Callable

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agent.safety.input_guard import REFUSALS, InputGuard
from agent.safety.pii import scrub_text
from agent.users import User, UserDirectory

logger = logging.getLogger(__name__)

HISTORY_MESSAGES = 4  # recent messages given to the input classifier for context
HISTORY_CHARS = 300


class AgentState(MessagesState):
    blocked: bool


def _recent_history(messages: list[AnyMessage]) -> str:
    recent = [m for m in messages if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and m.text)]
    return "\n".join(f"{m.type}: {m.text[:HISTORY_CHARS]}" for m in recent[-HISTORY_MESSAGES:])


def build_graph(
    llm: Runnable,
    tools: list[BaseTool],
    system_prompt: Callable[[User], str],
    input_guard: InputGuard,
    users: UserDirectory,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:

    def check_input(state: AgentState, config: RunnableConfig) -> dict:
        *previous, message = state["messages"]
        verdict = input_guard.check(message.text, _recent_history(previous))
        if verdict.allowed:
            return {"blocked": False}
        logger.warning("input blocked user=%s category=%s source=%s",
                       config["configurable"]["user_id"], verdict.category, verdict.source)
        return {"blocked": True, "messages": [AIMessage(REFUSALS[verdict.category])]}

    def agent(state: AgentState, config: RunnableConfig) -> dict:
        user = users.get(config["configurable"]["user_id"])
        response = llm.invoke([SystemMessage(system_prompt(user)), *state["messages"]])
        return {"messages": [response]}

    def check_output(state: AgentState) -> dict:
        message = state["messages"][-1]
        clean, found = scrub_text(message.text)
        if not found:
            return {}
        logger.warning("output scrubbed: %s", found)
        # Same id: replaces the message in the history, so the PII is not kept either.
        return {"messages": [AIMessage(clean, id=message.id)]}

    graph = StateGraph(AgentState)
    graph.add_node("input_guard", check_input)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("output_guard", check_output)

    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges("input_guard", lambda s: END if s["blocked"] else "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "output_guard"})
    graph.add_edge("tools", "agent")
    graph.add_edge("output_guard", END)
    return graph.compile(checkpointer=checkpointer)
