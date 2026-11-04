"""Dataset schema loaded from config/schema.yaml (columns, descriptions, PII flags)."""
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import yaml

from agent.config import CONFIG_DIR


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str
    pii: bool


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: tuple[Column, ...]

    @property
    def public_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if not c.pii)


class Schema:
    def __init__(self, path: Path = CONFIG_DIR / "schema.yaml") -> None:
        raw = yaml.safe_load(path.read_text())
        self.dataset: str = raw["dataset"]
        self.tables: dict[str, Table] = {
            name: Table(
                name=name,
                description=t.get("description", ""),
                columns=tuple(Column(c["name"], c["type"], c.get("description", ""), c["pii"])
                              for c in t["columns"]),
            )
            for name, t in raw["tables"].items()
        }

    def fqn(self, table: str) -> str:
        """Fully qualified BigQuery table name."""
        return f"`{self.dataset}.{table}`"

    @cached_property
    def pii_columns(self) -> dict[str, frozenset[str]]:
        return {t.name: frozenset(c.name for c in t.columns if c.pii) for t in self.tables.values()}

    def to_prompt(self) -> str:
        """Compact schema description for the LLM. PII columns are omitted entirely."""
        lines = []
        for t in self.tables.values():
            lines.append(f"### {self.fqn(t.name)}\n{t.description}")
            lines += [f"- {c.name} ({c.type}): {c.description}" for c in t.public_columns]
            lines.append("")
        return "\n".join(lines).strip()
