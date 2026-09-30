#!/usr/bin/env python
"""CrewAI Flow: user requirement → Orchestrator crew → PostgreSQL."""

from __future__ import annotations

import json
import os
import sys

from pydantic import BaseModel, Field

from crewai.flow import Flow, listen, start

from form_db_flow.runner import run_db_builder


class DbBuilderState(BaseModel):
    requirement: str = ""
    status: str = "pending"
    schema_json: dict = Field(default_factory=dict)
    created_tables: list[str] = Field(default_factory=list)
    tools_db_id: str | None = None
    message: str = ""
    creation_summary: dict = Field(default_factory=dict)


class DbBuilderFlow(Flow[DbBuilderState]):
    """Single flow wrapping the 1-crew / 3-agent DB builder."""

    @start()
    def collect_requirement(self, crewai_trigger_payload: dict | None = None):
        if crewai_trigger_payload and crewai_trigger_payload.get("requirement"):
            self.state.requirement = str(crewai_trigger_payload["requirement"]).strip()
            print(f"Using trigger requirement: {self.state.requirement[:120]}...")
            return

        if self.state.requirement:
            return

        print("Enter a database requirement (or pass via trigger payload):")
        try:
            text = input("> ").strip()
        except EOFError:
            text = ""
        if not text:
            text = (
                "Create an employee management database with employees, "
                "departments, attendance and salaries."
            )
            print(f"Using default example:\n{text}")
        self.state.requirement = text

    @listen(collect_requirement)
    def run_crew(self):
        print("\n=== Starting Orchestrator DB Builder Crew ===\n")
        owner = (
            os.getenv("TOOLS_OWNER_USER_ID", "").strip()
            or "cli-local"
        )
        outcome = run_db_builder(self.state.requirement, owner_user_id=owner)
        self.state.status = outcome.get("status", "completed")
        self.state.schema_json = outcome.get("schema") or {}
        self.state.created_tables = outcome.get("created_tables") or []
        self.state.tools_db_id = outcome.get("tools_db_id")
        self.state.message = outcome.get("message") or ""
        self.state.creation_summary = outcome.get("creation_summary") or {}
        return outcome

    @listen(run_crew)
    def print_result(self):
        print("\n=== Final Result ===\n")
        print(self.state.message)
        print("\n--- Summary ---")
        print(f"status: {self.state.status}")
        print(f"tools_db_id: {self.state.tools_db_id}")
        print(f"created_tables: {self.state.created_tables}")
        if self.state.schema_json:
            print(
                "feature_name:",
                self.state.schema_json.get("feature_name"),
            )


def kickoff(requirement: str | None = None):
    flow = DbBuilderFlow()
    if requirement:
        flow.state.requirement = requirement
    return flow.kickoff()


def plot():
    DbBuilderFlow().plot()


def run_with_trigger():
    if len(sys.argv) < 2:
        raise Exception(
            "No trigger payload provided. Please provide JSON payload as argument."
        )
    try:
        trigger_payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        raise Exception("Invalid JSON payload provided as argument") from exc

    flow = DbBuilderFlow()
    return flow.kickoff({"crewai_trigger_payload": trigger_payload})


if __name__ == "__main__":
    # Support: python -m form_db_flow.main
    # or: python main.py "Create employees database..."
    arg_req = " ".join(sys.argv[1:]).strip() or None
    kickoff(arg_req)
