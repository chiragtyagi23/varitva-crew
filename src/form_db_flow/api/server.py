"""Minimal HTTP API so Nest Tools can kick off the Orchestrator crew."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

load_dotenv()

from form_db_flow.runner import run_db_builder  # noqa: E402
from form_db_flow.tools.postgres_safe_tool import (  # noqa: E402
    get_tools_db_feature,
    list_tools_db_features,
)


class KickoffRequest(BaseModel):
    requirement: str = Field(..., min_length=3, max_length=8000)
    owner_user_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Nest/voicera user id that owns this feature",
    )


class KickoffResponse(BaseModel):
    status: str
    requirement: str
    schema_json: dict | None = None
    created_tables: list[str] = Field(default_factory=list)
    tools_db_id: str | None = None
    message: str = ""
    creation_summary: dict | None = None


app = FastAPI(title="Form DB Flow — DB Builder", version="0.1.0")


@app.get("/health")
def health():
    return {"ok": True, "service": "form_db_flow"}


@app.get("/v1/db-builder/features")
def list_features(
    owner_user_id: str = Query(..., min_length=1, max_length=128),
):
    """List features owned by one Nest user (tools_db.owner_user_id)."""
    try:
        return {"features": list_tools_db_features(owner_user_id=owner_user_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list features: {exc}",
        ) from exc


@app.get("/v1/db-builder/features/{feature_id}")
def get_feature(
    feature_id: str,
    owner_user_id: str = Query(..., min_length=1, max_length=128),
):
    try:
        row = get_tools_db_feature(feature_id, owner_user_id=owner_user_id)
        if not row:
            raise HTTPException(status_code=404, detail="Feature not found")
        return row
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get feature: {exc}",
        ) from exc


@app.post("/v1/db-builder/kickoff", response_model=KickoffResponse)
def kickoff(body: KickoffRequest):
    try:
        outcome = run_db_builder(
            body.requirement,
            owner_user_id=body.owner_user_id,
        )
        return KickoffResponse(
            status=outcome.get("status", "completed"),
            requirement=outcome.get("requirement", body.requirement),
            schema_json=outcome.get("schema"),
            created_tables=outcome.get("created_tables") or [],
            tools_db_id=outcome.get("tools_db_id"),
            message=outcome.get("message") or "",
            creation_summary=outcome.get("creation_summary"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"DB builder failed: {exc}",
        ) from exc


def serve():
    import uvicorn

    port = int(os.getenv("FLOW_HTTP_PORT", "8090"))
    uvicorn.run(
        "form_db_flow.api.server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    serve()
