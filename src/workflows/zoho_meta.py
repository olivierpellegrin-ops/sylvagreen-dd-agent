"""Workflow zoho-meta : métadonnées des modules Zoho CRM (champs, picklists, layouts)."""

from __future__ import annotations

from typing import List, Optional

import mistralai.workflows as workflows
from mistralai.workflows import workflow
from pydantic import BaseModel, Field

# Les activites (qui importent httpx) doivent passer par imports_passed_through,
# sinon le sandbox Temporal tente de re-importer httpx et echoue.
with workflow.unsafe.imports_passed_through():
    from .zoho_activities import exchange_code, fetch_module_meta


class ZohoMetaInput(BaseModel):
    """Entrée : modules à décrire ; credentials optionnels pour l'échange de code."""

    modules: List[str] = Field(default_factory=lambda: ["Leads", "Deals"])
    code: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


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
        access_token = None
        refresh_token = None
        if input.code and input.client_id and input.client_secret:
            tok = await exchange_code(input.code, input.client_id, input.client_secret)
            access_token = tok["access_token"]
            refresh_token = tok["refresh_token"]
        resultats = {}
        for m in input.modules:
            resultats[m] = await fetch_module_meta(m, access_token)
        return {
            "refresh_token_obtenu": refresh_token,
            "modules": resultats,
        }
