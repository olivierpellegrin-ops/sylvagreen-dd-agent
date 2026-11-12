"""Workflow zoho-meta : métadonnées des modules Zoho CRM."""

from __future__ import annotations

from typing import List

import mistralai.workflows as workflows
from mistralai.workflows import workflow
from pydantic import BaseModel, Field

# Les activites (qui importent httpx) doivent passer par imports_passed_through,
# sinon le sandbox Temporal tente de re-importer httpx et echoue.
with workflow.unsafe.imports_passed_through():
    from .zoho_activities import fetch_module_meta


class ZohoMetaInput(BaseModel):
    """Entrée : la liste des modules à décrire (Leads, Deals...)."""

    modules: List[str] = Field(default_factory=lambda: ["Leads", "Deals"])


@workflows.workflow.define(
    name="zoho-meta",
    workflow_display_name="Zoho Meta",
    workflow_description=(
        "Récupère les noms API des champs, les valeurs de picklists et les "
        "dispositions des modules Zoho CRM (Leads, Deals...)."
    ),
)
class ZohoMetaWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: ZohoMetaInput) -> dict:
        resultats = {}
        for m in input.modules:
            resultats[m] = await fetch_module_meta(m)
        return resultats
