"""Workflow test-chercher : diagnostic des criteres de recherche Zoho (outil chercher_prospect).

Teste plusieurs variantes de criteria sur /Leads/search et renvoie les reponses brutes
pour identifier pourquoi la recherche par prenom+nom echoue.
"""

from __future__ import annotations

import mistralai.workflows as workflows
from mistralai.workflows import workflow
from pydantic import BaseModel

with workflow.unsafe.imports_passed_through():
    from .test_activities import tester_criteres


class TestChercherInput(BaseModel):
    """Requete telle qu'Olivier la formule (ex. 'Julien de Sloovere')."""

    requete: str = "Julien de Sloovere"


@workflows.workflow.define(
    name="test-chercher",
    workflow_display_name="Test chercher prospect",
    workflow_description="Diagnostic des criteres de recherche Zoho Leads/search.",
)
class TestChercherWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TestChercherInput) -> dict:
        return await tester_criteres(requete=input.requete)
