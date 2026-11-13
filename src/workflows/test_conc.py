"""Workflow test-concurrents : valeur exacte d'Industry des distributeurs."""

from __future__ import annotations

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .test_activities import sonder_recents


@workflows.workflow.define(
    name="test-concurrents",
    workflow_display_name="Test comptes distributeurs",
    workflow_description="Sonde les Industry des Accounts et leur GPS.",
)
class TestConcurrentsWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: dict) -> dict:
        return await sonder_recents()
