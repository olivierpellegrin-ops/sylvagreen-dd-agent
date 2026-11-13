"""Workflow test-preparer : execute le VRAI preparer_etude de l'agent sur une fiche."""

from __future__ import annotations

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .test_activities import executer_preparer


class TestPreparerInput(BaseModel):
    lead_id: str = "968010000002251002"
    variante: str = "societe"


@workflows.workflow.define(
    name="test-preparer",
    workflow_display_name="Test preparer_etude (outil agent)",
    workflow_description="Importe agent/zoho_tools.py et execute preparer_etude sur une fiche reelle.",
)
class TestPreparerWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TestPreparerInput) -> dict:
        return await executer_preparer(lead_id=input.lead_id, variante=input.variante)
