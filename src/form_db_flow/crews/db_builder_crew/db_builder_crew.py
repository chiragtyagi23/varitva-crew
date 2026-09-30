from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task


@CrewBase
class DbBuilderCrew:
    """
    Design crew only (2 agents / 2 tasks).

    Schema Designer → Orchestrator validate.
    Actual Postgres provisioning runs in runner.provision_schema() so we do
    not rely on the LLM filling tool payloads (which often came through as {}).
    """

    agents: list[BaseAgent]
    tasks: list[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def orchestrator(self) -> Agent:
        return Agent(
            config=self.agents_config["orchestrator"],  # type: ignore[index]
            tools=[],
            allow_delegation=False,
            verbose=True,
        )

    @agent
    def schema_designer(self) -> Agent:
        return Agent(
            config=self.agents_config["schema_designer"],  # type: ignore[index]
            tools=[],
            allow_delegation=False,
            verbose=True,
        )

    @task
    def design_schema_task(self) -> Task:
        return Task(
            config=self.tasks_config["design_schema_task"],  # type: ignore[index]
        )

    @task
    def validate_and_handoff_task(self) -> Task:
        return Task(
            config=self.tasks_config["validate_and_handoff_task"],  # type: ignore[index]
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )
