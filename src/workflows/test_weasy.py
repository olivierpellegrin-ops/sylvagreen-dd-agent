"""Workflow test-weasy : le gabarit d'étude rend-il en PDF avec la WeasyPrint du workflow ?"""

from __future__ import annotations

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .test_activities import tester_weasy


class TestWeasyInput(BaseModel):
    variante: str = "societe"


@workflows.workflow.define(
    name="test-weasy",
    workflow_display_name="Test rendu etude WeasyPrint",
    workflow_description="Rend le gabarit d'etude en PDF et rapporte la version WeasyPrint.",
)
class TestWeasyWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TestWeasyInput) -> dict:
        return await tester_weasy(variante=input.variante)
