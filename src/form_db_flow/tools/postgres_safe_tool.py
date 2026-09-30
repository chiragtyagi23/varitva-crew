"""Safe PostgreSQL tool for CrewAI DB Creation Agent.

Connects via DATABASE_URL. Blocks DROP / TRUNCATE / DELETE.
Uses transactions and rolls back on failure.
Also bootstraps and updates the tools_db registry table.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Type

from pydantic import BaseModel, Field
from crewai.tools import BaseTool

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore


FORBIDDEN_SQL = re.compile(
    r"(?<!\bON\s)\b(DROP|TRUNCATE|DELETE)\b|\bALTER\s+TABLE\s+\S+\s+DROP\b",
    re.IGNORECASE,
)


def _assert_safe_sql(sql: str) -> None:
    if ";" in sql.strip().rstrip(";"):
        # Allow a single trailing semicolon only
        stripped = sql.strip()
        if stripped.count(";") > 1 or not stripped.endswith(";"):
            raise ValueError("Multiple SQL statements are not allowed")
    # Normalize so "ON DELETE" is not treated as destructive DELETE
    normalized = re.sub(r"\bON\s+DELETE\b", "ON_DELETE", sql, flags=re.IGNORECASE)
    if FORBIDDEN_SQL.search(normalized):
        raise ValueError(
            "Destructive SQL is forbidden (DROP / TRUNCATE / DELETE / DROP COLUMN)"
        )


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set in the environment")
    return url


def _connect():
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Run: uv add psycopg[binary]")
    return psycopg.connect(_database_url(), row_factory=dict_row)


def ensure_tools_db_table() -> None:
    """Create the tools_db registry table if missing (always in public)."""
    ddl = """
    CREATE TABLE IF NOT EXISTS public.tools_db (
        id UUID PRIMARY KEY,
        owner_user_id TEXT,
        feature_name TEXT,
        requirement_text TEXT,
        schema_json JSONB,
        status TEXT NOT NULL DEFAULT 'pending',
        created_tables TEXT[] DEFAULT ARRAY[]::TEXT[],
        error_message TEXT,
        pg_schema TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with _connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(
                    """
                    ALTER TABLE public.tools_db
                    ADD COLUMN IF NOT EXISTS pg_schema TEXT
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE public.tools_db
                    ADD COLUMN IF NOT EXISTS owner_user_id TEXT
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS tools_db_owner_user_id_idx
                    ON public.tools_db (owner_user_id)
                    """
                )


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
        raise ValueError(f"Invalid SQL identifier: {name}")
    return f'"{name}"'


def feature_schema_name(feature_name: str) -> str:
    """Sanitize feature_name into a safe PostgreSQL schema name (never public)."""
    raw = (feature_name or "feature").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_]+", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "feature"
    if not re.match(r"^[a-z]", cleaned):
        cleaned = f"feat_{cleaned}"
    reserved = {"public", "information_schema", "pg_catalog", "pg_toast"}
    if cleaned in reserved or cleaned.startswith("pg_"):
        cleaned = f"feat_{cleaned}"
    return cleaned[:63]


def normalize_ddl_schema(name: str | None) -> str:
    """Allow explicit 'public'; otherwise treat as a feature schema name."""
    if name is None or str(name).strip() == "" or str(name).strip().lower() == "public":
        return "public"
    return feature_schema_name(str(name))


def ensure_feature_schema(schema_name: str) -> str:
    """Create a dedicated schema for one feature (folder in DBeaver)."""
    schema_name = feature_schema_name(schema_name)
    sql = f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema_name)}"
    _assert_safe_sql(sql)
    with _connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
    return schema_name


def _qualify(schema: str, table: str) -> str:
    return f"{_quote_ident(schema)}.{_quote_ident(table)}"


def _map_type(pg_type: str) -> str:
    allowed = {
        "UUID",
        "TEXT",
        "VARCHAR",
        "INTEGER",
        "BIGINT",
        "BOOLEAN",
        "TIMESTAMPTZ",
        "TIMESTAMP",
        "DATE",
        "NUMERIC",
        "JSONB",
        "REAL",
        "DOUBLE PRECISION",
    }
    t = pg_type.strip().upper()
    # Allow VARCHAR(n)
    if re.fullmatch(r"VARCHAR\(\d+\)", t):
        return t
    if t not in allowed:
        # Soft-allow common aliases
        aliases = {
            "STRING": "TEXT",
            "STR": "TEXT",
            "INT": "INTEGER",
            "BOOL": "BOOLEAN",
            "JSON": "JSONB",
            "DATETIME": "TIMESTAMPTZ",
        }
        t = aliases.get(t, t)
        if t not in allowed and not re.fullmatch(r"VARCHAR\(\d+\)", t):
            raise ValueError(f"Unsupported column type: {pg_type}")
    return t


class PostgresSafeToolInput(BaseModel):
    action: str = Field(
        ...,
        description=(
            "One of: list_tables, get_table_schema, create_table, add_column, "
            "create_foreign_key, create_constraint, create_index, execute_safe_sql, "
            "apply_schema, registry_create, registry_update"
        ),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific JSON payload",
    )


class PostgresSafeTool(BaseTool):
    name: str = "postgres_safe"
    description: str = (
        "Safe PostgreSQL tool. Can list/describe tables, create tables/columns/"
        "FKs/constraints/indexes, apply a structured schema, and update tools_db "
        "registry. Destructive SQL (DROP/TRUNCATE/DELETE) is blocked. "
        "Uses DATABASE_URL and transactions with rollback on failure."
    )
    args_schema: Type[BaseModel] = PostgresSafeToolInput

    def _run(self, action: str, payload: Optional[dict[str, Any]] = None) -> str:
        payload = dict(payload or {})
        # LLMs sometimes put registry id on the wrong key or outside nested fields
        if action == "registry_update":
            if "id" not in payload:
                for alt in ("tools_db_id", "registry_id"):
                    if alt in payload:
                        payload["id"] = payload[alt]
                        break
        try:
            ensure_tools_db_table()
            handler = {
                "list_tables": self._list_tables,
                "get_table_schema": self._get_table_schema,
                "create_table": self._create_table,
                "add_column": self._add_column,
                "create_foreign_key": self._create_foreign_key,
                "create_constraint": self._create_constraint,
                "create_index": self._create_index,
                "execute_safe_sql": self._execute_safe_sql,
                "apply_schema": self._apply_schema,
                "registry_create": self._registry_create,
                "registry_update": self._registry_update,
            }.get(action)
            if not handler:
                return json.dumps({"ok": False, "error": f"Unknown action: {action}"})
            result = handler(payload)
            return json.dumps({"ok": True, "result": result}, default=str)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)})

    def _list_tables(self, payload: dict[str, Any]) -> list[str]:
        schema = normalize_ddl_schema(payload.get("schema") or payload.get("pg_schema"))
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    """,
                    (schema,),
                )
                rows = cur.fetchall()
                return [r["table_name"] for r in rows]

    def _get_table_schema(self, payload: dict[str, Any]) -> dict[str, Any]:
        table = payload.get("table_name") or payload.get("table")
        if not table:
            raise ValueError("table_name is required")
        schema = normalize_ddl_schema(payload.get("schema") or payload.get("pg_schema"))
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (schema, table),
                )
                columns = cur.fetchall()
                return {"schema": schema, "table": table, "columns": columns}

    def _table_exists(self, cur, schema: str, table: str) -> bool:
        cur.execute("SELECT to_regclass(%s) AS reg", (f"{schema}.{table}",))
        row = cur.fetchone()
        return bool(row and row["reg"])

    def _create_table(self, payload: dict[str, Any]) -> dict[str, Any]:
        table = payload["name"]
        schema = normalize_ddl_schema(payload.get("schema"))
        columns = payload.get("columns") or []
        if not columns:
            raise ValueError("columns required")

        col_defs: list[str] = []
        pk_cols: list[str] = []
        for col in columns:
            name = col["name"]
            ctype = _map_type(col.get("type", "TEXT"))
            parts = [_quote_ident(name), ctype]
            if col.get("primary_key"):
                pk_cols.append(name)
            if not col.get("nullable", True) and not col.get("primary_key"):
                parts.append("NOT NULL")
            if col.get("unique") and not col.get("primary_key"):
                parts.append("UNIQUE")
            if col.get("default"):
                default = str(col["default"])
                if not re.fullmatch(
                    r"(NOW\(\)|CURRENT_TIMESTAMP|TRUE|FALSE|NULL|gen_random_uuid\(\)|'[A-Za-z0-9_\-:. ]*'|\d+(\.\d+)?)",
                    default,
                    re.IGNORECASE,
                ):
                    raise ValueError(f"Unsafe default: {default}")
                parts.append(f"DEFAULT {default}")
            col_defs.append(" ".join(parts))

        if pk_cols:
            col_defs.append(
                f"PRIMARY KEY ({', '.join(_quote_ident(c) for c in pk_cols)})"
            )

        sql = (
            f"CREATE TABLE IF NOT EXISTS {_qualify(schema, table)} (\n  "
            + ",\n  ".join(col_defs)
            + "\n)"
        )
        _assert_safe_sql(sql)

        with _connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    existed = self._table_exists(cur, schema, table)
                    cur.execute(sql)
                    return {
                        "schema": schema,
                        "table": table,
                        "created": not existed,
                        "already_existed": existed,
                        "sql": sql,
                    }

    def _add_column(self, payload: dict[str, Any]) -> dict[str, Any]:
        table = payload["table_name"]
        schema = normalize_ddl_schema(payload.get("schema"))
        col = payload["column"]
        name = col["name"]
        ctype = _map_type(col.get("type", "TEXT"))
        nullable = col.get("nullable", True)
        sql = (
            f"ALTER TABLE {_qualify(schema, table)} "
            f"ADD COLUMN IF NOT EXISTS {_quote_ident(name)} {ctype}"
        )
        if not nullable:
            sql += " NOT NULL"
        _assert_safe_sql(sql)
        with _connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql)
        return {"schema": schema, "table": table, "column": name, "sql": sql}

    def _create_foreign_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        table = payload["table_name"]
        schema = normalize_ddl_schema(payload.get("schema"))
        column = payload["column"]
        ref_table = payload["references_table"]
        ref_schema = normalize_ddl_schema(
            payload.get("references_schema") or payload.get("schema")
        )
        ref_column = payload.get("references_column", "id")
        on_delete = (payload.get("on_delete") or "RESTRICT").upper()
        if on_delete not in {"RESTRICT", "CASCADE", "SET NULL", "NO ACTION"}:
            raise ValueError(f"Invalid on_delete: {on_delete}")
        fk_name = payload.get("name") or f"fk_{table}_{column}"
        sql = (
            f"ALTER TABLE {_qualify(schema, table)} "
            f"ADD CONSTRAINT {_quote_ident(fk_name)} "
            f"FOREIGN KEY ({_quote_ident(column)}) "
            f"REFERENCES {_qualify(ref_schema, ref_table)} ({_quote_ident(ref_column)}) "
            f"ON DELETE {on_delete}"
        )
        _assert_safe_sql(sql)
        with _connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM information_schema.table_constraints
                        WHERE table_schema = %s
                          AND table_name = %s
                          AND constraint_name = %s
                        """,
                        (schema, table, fk_name),
                    )
                    if cur.fetchone():
                        return {"skipped": True, "reason": "fk_exists", "name": fk_name}
                    cur.execute(sql)
        return {"created": True, "name": fk_name, "schema": schema, "sql": sql}

    def _create_constraint(self, payload: dict[str, Any]) -> dict[str, Any]:
        table = payload["table_name"]
        schema = normalize_ddl_schema(payload.get("schema"))
        name = payload["name"]
        definition = payload["definition"]
        normalized = re.sub(r"\bON\s+DELETE\b", "ON_DELETE", definition, flags=re.IGNORECASE)
        if FORBIDDEN_SQL.search(normalized):
            raise ValueError("Forbidden SQL in constraint definition")
        if not re.fullmatch(
            r"(CHECK\s*\(.+\)|UNIQUE\s*\(.+\))",
            definition.strip(),
            re.IGNORECASE | re.DOTALL,
        ):
            raise ValueError("Only CHECK (...) or UNIQUE (...) constraints allowed")
        sql = (
            f"ALTER TABLE {_qualify(schema, table)} "
            f"ADD CONSTRAINT {_quote_ident(name)} {definition}"
        )
        _assert_safe_sql(sql)
        with _connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM information_schema.table_constraints
                        WHERE table_schema = %s
                          AND table_name = %s
                          AND constraint_name = %s
                        """,
                        (schema, table, name),
                    )
                    if cur.fetchone():
                        return {"skipped": True, "reason": "exists", "name": name}
                    cur.execute(sql)
        return {"created": True, "name": name, "schema": schema, "sql": sql}

    def _create_index(self, payload: dict[str, Any]) -> dict[str, Any]:
        table = payload["table_name"]
        schema = normalize_ddl_schema(payload.get("schema"))
        name = payload["name"]
        columns = payload["columns"]
        unique = bool(payload.get("unique"))
        cols = ", ".join(_quote_ident(c) for c in columns)
        unique_sql = "UNIQUE " if unique else ""
        # Index names are DB-wide; prefix with schema to avoid clashes
        index_name = name if name.startswith(f"{schema}_") else f"{schema}_{name}"
        index_name = re.sub(r"[^a-zA-Z0-9_]", "_", index_name)[:63]
        sql = (
            f"CREATE {unique_sql}INDEX IF NOT EXISTS {_quote_ident(index_name)} "
            f"ON {_qualify(schema, table)} ({cols})"
        )
        _assert_safe_sql(sql)
        with _connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql)
        return {"created": True, "name": index_name, "schema": schema, "sql": sql}

    def _execute_safe_sql(self, payload: dict[str, Any]) -> dict[str, Any]:
        sql = payload.get("sql") or ""
        _assert_safe_sql(sql)
        # Only allow CREATE / ALTER ADD / SELECT
        if not re.match(
            r"^\s*(CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)|ALTER\s+TABLE|SELECT)\b",
            sql,
            re.IGNORECASE,
        ):
            raise ValueError("Only CREATE TABLE/INDEX, ALTER TABLE ADD, SELECT allowed")
        with _connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description:
                        rows = cur.fetchall()
                        return {"rows": rows}
                    return {"rowcount": cur.rowcount}

    def _apply_schema(self, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload.get("schema") or payload
        tables = schema.get("tables") or []
        feature_name = schema.get("feature_name", "feature")
        pg_schema = ensure_feature_schema(
            payload.get("pg_schema") or feature_name
        )
        created: list[str] = []
        details: list[dict[str, Any]] = []

        # Pass 1: create tables inside feature schema (no FKs yet)
        for table in tables:
            table_payload = {**table, "schema": pg_schema}
            result = self._create_table(table_payload)
            details.append(result)
            if result.get("created") or result.get("already_existed"):
                created.append(table["name"])

        # Pass 2: FKs, constraints, indexes (same schema) — soft-fail so
        # a bad index does not discard an otherwise successful feature schema.
        warnings: list[str] = []
        for table in tables:
            tname = table["name"]
            for fk in table.get("foreign_keys") or []:
                try:
                    details.append(
                        self._create_foreign_key(
                            {
                                "schema": pg_schema,
                                "table_name": tname,
                                "column": fk["column"],
                                "references_schema": pg_schema,
                                "references_table": fk["references_table"],
                                "references_column": fk.get("references_column", "id"),
                                "on_delete": fk.get("on_delete", "RESTRICT"),
                                "name": fk.get("name"),
                            }
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"fk {tname}.{fk.get('column')}: {exc}")
                    details.append({"ok": False, "step": "foreign_key", "error": str(exc)})
            for cons in table.get("constraints") or []:
                try:
                    details.append(
                        self._create_constraint(
                            {
                                "schema": pg_schema,
                                "table_name": tname,
                                "name": cons["name"],
                                "definition": cons["definition"],
                            }
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"constraint {cons.get('name')}: {exc}")
                    details.append({"ok": False, "step": "constraint", "error": str(exc)})
            for idx in table.get("indexes") or []:
                try:
                    details.append(
                        self._create_index(
                            {
                                "schema": pg_schema,
                                "table_name": tname,
                                "name": idx["name"],
                                "columns": idx["columns"],
                                "unique": idx.get("unique", False),
                            }
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"index {idx.get('name')}: {exc}")
                    details.append({"ok": False, "step": "index", "error": str(exc)})

        # Prefer live tables in the schema (handles IF NOT EXISTS re-runs)
        live = tables_in_schema(pg_schema)
        if live:
            created = live

        return {
            "feature_name": feature_name,
            "pg_schema": pg_schema,
            "created_tables": created,
            "qualified_tables": [f"{pg_schema}.{t}" for t in created],
            "details": details,
            "warnings": warnings,
        }

    def _registry_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        row_id = payload.get("id") or str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with _connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.tools_db (
                            id, owner_user_id, feature_name, requirement_text, schema_json,
                            status, created_tables, error_message, pg_schema,
                            created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            row_id,
                            payload.get("owner_user_id"),
                            payload.get("feature_name"),
                            payload.get("requirement_text"),
                            json.dumps(payload.get("schema_json")),
                            payload.get("status", "pending"),
                            payload.get("created_tables") or [],
                            payload.get("error_message"),
                            payload.get("pg_schema"),
                            now,
                            now,
                        ),
                    )
        return {
            "id": row_id,
            "status": payload.get("status", "pending"),
            "pg_schema": payload.get("pg_schema"),
            "owner_user_id": payload.get("owner_user_id"),
        }

    def _registry_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        row_id = (
            payload.get("id")
            or payload.get("tools_db_id")
            or payload.get("registry_id")
        )
        if not row_id:
            raise ValueError(
                "registry_update requires payload.id (or tools_db_id) from registry_create"
            )
        fields: list[str] = []
        values: list[Any] = []
        for key, col in [
            ("feature_name", "feature_name"),
            ("requirement_text", "requirement_text"),
            ("status", "status"),
            ("error_message", "error_message"),
            ("pg_schema", "pg_schema"),
        ]:
            if key in payload:
                fields.append(f"{col} = %s")
                values.append(payload[key])
        if "schema_json" in payload:
            fields.append("schema_json = %s::jsonb")
            values.append(json.dumps(payload["schema_json"]))
        if "created_tables" in payload:
            fields.append("created_tables = %s")
            values.append(payload["created_tables"])
        if len(fields) == 0:
            raise ValueError(
                "registry_update needs at least one of: status, created_tables, "
                "schema_json, feature_name, error_message, pg_schema"
            )
        fields.append("updated_at = %s")
        values.append(datetime.now(timezone.utc))
        values.append(row_id)
        sql = f"UPDATE public.tools_db SET {', '.join(fields)} WHERE id = %s"
        with _connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql, values)
                    if cur.rowcount == 0:
                        raise ValueError(f"No tools_db row found for id={row_id}")
        return {"id": row_id, "updated": True}


def tables_in_schema(schema_name: str) -> list[str]:
    """List base tables in a feature schema."""
    name = "public" if schema_name == "public" else feature_schema_name(schema_name)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """,
                (name,),
            )
            return [r["table_name"] for r in cur.fetchall()]


def _row_to_feature(row: dict[str, Any]) -> dict[str, Any]:
    schema = row.get("schema_json")
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError:
            schema = None
    created = row.get("created_tables") or []
    if not isinstance(created, list):
        created = list(created) if created else []
    feature_name = row.get("feature_name") or (
        schema.get("feature_name") if isinstance(schema, dict) else None
    ) or "feature"
    pg_schema = row.get("pg_schema") or (
        feature_schema_name(feature_name) if feature_name else None
    )
    req = row.get("requirement_text") or ""
    status = row.get("status") or "unknown"
    qualified = [f"{pg_schema}.{t}" for t in created] if pg_schema else created
    message = row.get("error_message") or (
        f"Feature '{feature_name}' in schema '{pg_schema}' — status={status}. "
        f"Tables: {', '.join(qualified) or '—'}."
    )
    return {
        "id": str(row["id"]),
        "feature_name": feature_name,
        "pg_schema": pg_schema,
        "owner_user_id": row.get("owner_user_id"),
        "saved_at": (
            row["created_at"].isoformat()
            if hasattr(row.get("created_at"), "isoformat")
            else str(row.get("created_at") or "")
        ),
        "result": {
            "status": status,
            "requirement": req,
            "schema_json": schema,
            "created_tables": created,
            "pg_schema": pg_schema,
            "tools_db_id": str(row["id"]),
            "message": message,
            "creation_summary": {
                "status": status,
                "tools_db_id": str(row["id"]),
                "feature_name": feature_name,
                "pg_schema": pg_schema,
                "created_tables": created,
                "qualified_tables": qualified,
            },
        },
    }


def list_tools_db_features(
    limit: int = 100,
    owner_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read feature builds from local tools_db, scoped to one Nest user when set."""
    if not owner_user_id or not str(owner_user_id).strip():
        raise ValueError("owner_user_id is required to list features")
    ensure_tools_db_table()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, owner_user_id, feature_name, requirement_text, schema_json,
                       status, created_tables, error_message, pg_schema, created_at
                FROM public.tools_db
                WHERE owner_user_id = %s
                  AND lower(coalesce(status, '')) IN ('created', 'completed')
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(owner_user_id).strip(), limit),
            )
            rows = cur.fetchall()
    return [_row_to_feature(r) for r in rows]


def get_tools_db_feature(
    feature_id: str,
    owner_user_id: str | None = None,
) -> dict[str, Any] | None:
    if not owner_user_id or not str(owner_user_id).strip():
        raise ValueError("owner_user_id is required to get a feature")
    ensure_tools_db_table()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, owner_user_id, feature_name, requirement_text, schema_json,
                       status, created_tables, error_message, pg_schema, created_at
                FROM public.tools_db
                WHERE id = %s AND owner_user_id = %s
                """,
                (feature_id, str(owner_user_id).strip()),
            )
            row = cur.fetchone()
    return _row_to_feature(row) if row else None

