"""LangGraph agent graph.

    START -> input_guard -> agent <-> tools
                 |            |         |
              (blocked)       |    (budget spent or
                 |            |     warehouse down)
                 |            v         v
                 |       output_guard <- give_up
                 v            |
                END <---------+

The delete confirmation node from docs/architecture.md §3 is added in a later step.
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

from agent.resilience import (
    StopReason,
    TurnLimits,
    close_dangling_tool_calls,
    current_turn,
    sql_statuses,
    stop_reason,
)
from agent.safety.input_guard import REFUSALS, InputGuard
from agent.safety.pii import scrub_text
from agent.users import User, UserDirectory

logger = logging.getLogger(__name__)

HISTORY_MESSAGES = 4  # recent messages given to the input classifier for context
HISTORY_CHARS = 300

MODELS_DOWN_MESSAGE = (
    "I can't reach the AI service right now, so I couldn't answer that. "
    "Your conversation is saved. Please try again in a minute."
)
GIVE_UP_MESSAGES = {
    StopReason.SQL_FAILURES: "I couldn't get a working query for this after several attempts, so I'd rather not "
                             "guess. Could you rephrase it, or narrow it to a specific period or product group?",
    StopReason.LLM_CALLS: "This needed more steps than I can take in one go. Could you split it into smaller questions?",
    StopReason.TOKENS: "This needed more analysis than I can do in one go. Could you split it into smaller questions?",
    StopReason.WAREHOUSE_UNAVAILABLE: "The data warehouse is unavailable right now, so I can't run this analysis. "
                                      "Please try again in a few minutes.",
}
PARTIAL_ANSWER_INSTRUCTION = """

## Finish now
You cannot run more queries for this question. Answer using only the results already obtained in
this conversation. Say clearly which parts of the question you could not answer."""


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
    summary_llm: Runnable | None = None,
    limits: TurnLimits = TurnLimits(),
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
        history = close_dangling_tool_calls(state["messages"])
        try:
            return {"messages": [llm.invoke([SystemMessage(system_prompt(user)), *history])]}
        except Exception as e:
            logger.error("no model could answer: %s", str(e)[:300])
            return {"messages": [AIMessage(MODELS_DOWN_MESSAGE)]}

    def after_tools(state: AgentState) -> str:
        return "give_up" if stop_reason(current_turn(state["messages"]), limits) else "agent"

    def give_up(state: AgentState, config: RunnableConfig) -> dict:
        turn = current_turn(state["messages"])
        reason = stop_reason(turn, limits)
        logger.warning("turn stopped early: %s", reason.value)
        if summary_llm is not None and "ok" in sql_statuses(turn):
            user = users.get(config["configurable"]["user_id"])
            prompt = system_prompt(user) + PARTIAL_ANSWER_INSTRUCTION
            try:
                return {"messages": [summary_llm.invoke([SystemMessage(prompt), *state["messages"]])]}
            except Exception as e:
                logger.error("could not summarise partial results: %s", str(e)[:300])
        return {"messages": [AIMessage(GIVE_UP_MESSAGES[reason])]}

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
    graph.add_node("give_up", give_up)
    graph.add_node("output_guard", check_output)

    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges("input_guard", lambda s: END if s["blocked"] else "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "output_guard"})
    graph.add_conditional_edges("tools", after_tools, ["agent", "give_up"])
    graph.add_edge("give_up", "output_guard")
    graph.add_edge("output_guard", END)
    return graph.compile(checkpointer=checkpointer)
