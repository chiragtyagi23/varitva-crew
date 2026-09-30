"""Shared structured schema contract for DB builder crew."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ColumnSpec(BaseModel):
    name: str
    type: str = Field(description="PostgreSQL type, e.g. UUID, TEXT, INTEGER, TIMESTAMPTZ")
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    default: Optional[str] = None


class ForeignKeySpec(BaseModel):
    column: str
    references_table: str
    references_column: str = "id"
    on_delete: str = "RESTRICT"


class ConstraintSpec(BaseModel):
    name: str
    type: str = Field(description="CHECK | UNIQUE | PRIMARY KEY")
    definition: str = Field(description="SQL fragment after CONSTRAINT name")


class IndexSpec(BaseModel):
    name: str
    columns: list[str]
    unique: bool = False


class TableSpec(BaseModel):
    name: str
    columns: list[ColumnSpec]
    foreign_keys: list[ForeignKeySpec] = Field(default_factory=list)
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    indexes: list[IndexSpec] = Field(default_factory=list)


class DatabaseSchema(BaseModel):
    feature_name: str
    tables: list[TableSpec]
    notes: list[str] = Field(default_factory=list)

    def model_dump_json_pretty(self) -> str:
        return self.model_dump_json(indent=2)


def parse_schema_json(raw: str | dict[str, Any]) -> DatabaseSchema:
    if isinstance(raw, dict):
        return DatabaseSchema.model_validate(raw)
    text = raw.strip()
    # Strip markdown fences if the LLM wraps JSON
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    return DatabaseSchema.model_validate_json(text)
