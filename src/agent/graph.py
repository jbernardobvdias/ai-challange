"""LangGraph agent graph.

    START -> agent -> (tool calls?) -> tools -> agent ... -> END

Guard, confirmation and fallback nodes from docs/architecture.md §3 are added in later steps.
"""
from langchain_core.messages import SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition


class AgentState(MessagesState):
    pass


def build_graph(
    llm: Runnable,
    tools: list[BaseTool],
    system_prompt: str,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:

    def agent(state: AgentState) -> dict:
        response = llm.invoke([SystemMessage(system_prompt), *state["messages"]])
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)
