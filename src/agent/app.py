"""Builds the agent from settings: BigQuery runner, schema, tools, LLM and graph."""
import logging
import warnings

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from agent.bigquery_runner import BigQueryRunner
from agent.config import Settings
from agent.graph import build_graph
from agent.llm import build_llm
from agent.prompts import build_system_prompt
from agent.schema import Schema
from agent.tools.sql import make_sql_tools


def build_agent(settings: Settings) -> CompiledStateGraph:
    # Results are small, so the REST download path is fine; silence the suggestion to install bqstorage.
    warnings.filterwarnings("ignore", message="BigQuery Storage module not found")

    schema = Schema()
    runner = BigQueryRunner(project_id=settings.gcp_project, dataset_id=schema.dataset)
    tools = make_sql_tools(runner, schema, max_rows=settings.max_result_rows)
    llm = build_llm(settings, tools)
    logging.getLogger(__name__).info("agent built: model=%s fallback=%s", settings.model, settings.fallback_model)
    return build_graph(llm, tools, build_system_prompt(schema), checkpointer=InMemorySaver())
