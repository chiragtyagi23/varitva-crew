"""Shared runner for CLI and HTTP kickoff."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from form_db_flow.crews.db_builder_crew.db_builder_crew import DbBuilderCrew
from form_db_flow.schema_models import DatabaseSchema, parse_schema_json
from form_db_flow.tools.postgres_safe_tool import (
    PostgresSafeTool,
    ensure_tools_db_table,
    feature_schema_name,
    tables_in_schema,
)

# Avoid interactive CrewAI trace prompts in HTTP/server mode
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")


def _extract_json_blob(text: str) -> dict[str, Any] | None:
    """Extract the first top-level JSON object from LLM text."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = text[start : i + 1]
                try:
                    parsed = json.loads(chunk)
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _coerce_schema(raw: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        try:
            return DatabaseSchema.model_validate(raw).model_dump()
        except Exception:
            return raw if "tables" in raw else None
    try:
        return parse_schema_json(raw).model_dump()
    except Exception:
        blob = _extract_json_blob(raw)
        if not blob:
            return None
        try:
            return DatabaseSchema.model_validate(blob).model_dump()
        except Exception:
            return blob if "tables" in blob else None


def provision_schema(
    requirement: str,
    schema: dict[str, Any],
    *,
    owner_user_id: str | None = None,
) -> dict[str, Any]:
    """
    Deterministically create registry row + domain tables in a per-feature schema.
    """
    tool = PostgresSafeTool()
    feature_name = schema.get("feature_name") or "feature"
    pg_schema = feature_schema_name(feature_name)
    tools_db_id: str | None = None

    try:
        created = tool._registry_create(
            {
                "feature_name": feature_name,
                "requirement_text": requirement,
                "schema_json": schema,
                "status": "designed",
                "pg_schema": pg_schema,
                "owner_user_id": owner_user_id,
            }
        )
        tools_db_id = created["id"]

        apply_result = tool._apply_schema(
            {"schema": schema, "pg_schema": pg_schema}
        )
        created_tables = apply_result.get("created_tables") or []
        pg_schema = apply_result.get("pg_schema") or pg_schema

        tool._registry_update(
            {
                "id": tools_db_id,
                "status": "created",
                "created_tables": created_tables,
                "schema_json": schema,
                "feature_name": feature_name,
                "pg_schema": pg_schema,
            }
        )

        qualified = apply_result.get("qualified_tables") or [
            f"{pg_schema}.{t}" for t in created_tables
        ]
        return {
            "status": "created",
            "tools_db_id": tools_db_id,
            "feature_name": feature_name,
            "pg_schema": pg_schema,
            "created_tables": created_tables,
            "apply_result": apply_result,
            "message": (
                f"Provisioned feature '{feature_name}' in schema '{pg_schema}'. "
                f"Tables: {', '.join(qualified) or '(none)'}."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        # If tables already exist in the feature schema, still mark created
        # so the Tools UI shows the card (index/FK issues shouldn't hide it).
        existing = tables_in_schema(pg_schema) if pg_schema else []
        if tools_db_id and existing:
            try:
                tool._registry_update(
                    {
                        "id": tools_db_id,
                        "status": "created",
                        "created_tables": existing,
                        "schema_json": schema,
                        "feature_name": feature_name,
                        "pg_schema": pg_schema,
                        "error_message": f"Partial warnings: {exc}",
                    }
                )
            except Exception:
                pass
            return {
                "status": "created",
                "tools_db_id": tools_db_id,
                "feature_name": feature_name,
                "pg_schema": pg_schema,
                "created_tables": existing,
                "apply_result": None,
                "message": (
                    f"Feature '{feature_name}' schema '{pg_schema}' has tables "
                    f"({', '.join(existing)}) with warnings: {exc}"
                ),
            }
        if tools_db_id:
            try:
                tool._registry_update(
                    {
                        "id": tools_db_id,
                        "status": "failed",
                        "error_message": str(exc),
                        "schema_json": schema,
                        "pg_schema": pg_schema,
                    }
                )
            except Exception:
                pass
        return {
            "status": "failed",
            "tools_db_id": tools_db_id,
            "feature_name": feature_name,
            "pg_schema": pg_schema,
            "created_tables": [],
            "apply_result": None,
            "message": f"Provisioning failed: {exc}",
        }


def run_db_builder(
    requirement: str,
    *,
    owner_user_id: str | None = None,
) -> dict[str, Any]:
    """Design schema with crew, then provision DB in Python (reliable)."""
    requirement = (requirement or "").strip()
    if not requirement:
        raise ValueError("requirement is required")
    if not owner_user_id or not str(owner_user_id).strip():
        raise ValueError("owner_user_id is required")

    ensure_tools_db_table()

    result = (
        DbBuilderCrew()
        .crew()
        .kickoff(inputs={"requirement": requirement})
    )

    tasks_output = getattr(result, "tasks_output", None) or []

    schema: dict[str, Any] | None = None
    # Task order: design=0, validate=1
    if len(tasks_output) >= 2:
        validated_raw = getattr(tasks_output[1], "raw", "") or ""
        schema = _coerce_schema(validated_raw)
    if schema is None and len(tasks_output) >= 1:
        design_raw = getattr(tasks_output[0], "raw", "") or ""
        schema = _coerce_schema(design_raw)

    if not schema or not schema.get("tables"):
        return {
            "status": "failed",
            "requirement": requirement,
            "schema": schema,
            "created_tables": [],
            "tools_db_id": None,
            "message": "Schema design/validation did not return a usable schema JSON.",
            "creation_summary": None,
        }

    creation = provision_schema(
        requirement,
        schema,
        owner_user_id=str(owner_user_id).strip(),
    )
    table_list = ", ".join(creation.get("created_tables") or []) or "—"
    report = (
        f"**Feature:** {creation.get('feature_name')}\n"
        f"**Status:** {creation.get('status')}\n"
        f"**tools_db id:** {creation.get('tools_db_id')}\n"
        f"**Created tables:** {table_list}\n\n"
        f"{creation.get('message')}\n"
    )

    return {
        "status": creation.get("status", "failed"),
        "requirement": requirement,
        "schema": schema,
        "created_tables": creation.get("created_tables") or [],
        "tools_db_id": creation.get("tools_db_id"),
        "message": report,
        "creation_summary": creation,
    }
