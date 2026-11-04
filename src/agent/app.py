"""Builds the agent from settings: BigQuery runner, schema, guards, tools, LLM and graph."""
import warnings
from functools import partial

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from agent.bigquery_runner import BigQueryRunner
from agent.config import Settings
from agent.graph import build_graph
from agent.llm import Models
from agent.prompts import build_system_prompt
from agent.safety.input_guard import InputGuard
from agent.safety.sql_guard import SqlGuard
from agent.schema import Schema
from agent.storage.reports_store import ReportStore
from agent.tools.reports import make_report_tools
from agent.tools.sql import make_sql_tools
from agent.users import UserDirectory
from agent.warehouse import Warehouse


def build_agent(settings: Settings, users: UserDirectory) -> CompiledStateGraph:
    # Results are small, so the REST download path is fine; silence the suggestion to install bqstorage.
    warnings.filterwarnings("ignore", message="BigQuery Storage module not found")

    schema = Schema()
    runner = BigQueryRunner(project_id=settings.gcp_project, dataset_id=schema.dataset)
    warehouse = Warehouse(runner.client, settings.max_query_bytes, settings.query_timeout_s)
    reports = ReportStore(settings.db_path)
    tools = [
        *make_sql_tools(warehouse, schema, SqlGuard(schema), users, max_rows=settings.max_result_rows),
        *make_report_tools(reports),
    ]
    models = Models(settings)
    return build_graph(
        llm=models.chat(tools),
        tools=tools,
        system_prompt=partial(build_system_prompt, schema),
        input_guard=InputGuard(models.classifier()),
        users=users,
        checkpointer=InMemorySaver(),
        reports=reports,
        summary_llm=models.chat(),
    )
