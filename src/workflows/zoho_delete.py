"""Workflow zoho-delete : suppression unitaire SECURISEE d'un enregistrement
Zoho par son id (un seul id par appel, module explicite). Usage de diagnostic :
nettoyage de comptes dupliques. Tout le reste (lecture/recherche) reste dans
zoho-query.
"""

from __future__ import annotations

import os

import mistralai.workflows as workflows
from mistralai.workflows import workflow
from pydantic import BaseModel

with workflow.unsafe.imports_passed_through():
    import httpx

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"


class ZohoDeleteInput(BaseModel):
    """Supprime UN enregistrement par son id Zoho. Irreversible.

    module : Accounts (defaut), Leads, Contacts, Deals...
    id_zoho : identifiant 18 chiffres de l'enregistrement a supprimer.
    dry_run : si true (defaut), NE SUPPRIME RIEN, verifie seulement la fiche.
    """

    module: str = "Accounts"
    id_zoho: str
    dry_run: bool = True


@workflows.activity()
async def supprimer_zoho(module: str, id_zoho: str, dry_run: bool) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        params = {
            "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN", ""),
            "client_id": os.environ.get("ZOHO_CLIENT_ID", ""),
            "client_secret": os.environ.get("ZOHO_CLIENT_SECRET", ""),
            "grant_type": "refresh_token",
        }
        t = await client.post(ZOHO_ACCOUNTS, params=params)
        body = t.json()
        if "access_token" not in body:
            raise ValueError(f"Refresh token Zoho refuse : HTTP {t.status_code} / {body}")
        headers = {"Authorization": "Zoho-oauthtoken " + body["access_token"]}
        champs = {"Accounts": "Account_Name,SIRET,Industry,Billing_City"}.get(module, "id")
        rv = await client.get(f"{ZOHO_CRM}/{module}/{id_zoho}", params={"fields": champs}, headers=headers)
        if rv.status_code != 200:
            return {"erreur": f"Fiche introuvable : HTTP {rv.status_code}", "corps": rv.text[:300]}
        fiche = rv.json().get("data", [{}])[0]
        if dry_run:
            return {"dry_run": True, "fiche": fiche, "action": "RIEN supprime"}
        rd = await client.delete(f"{ZOHO_CRM}/{module}/{id_zoho}", params={"with_trigger": "false"}, headers=headers)
        return {
            "dry_run": False,
            "http": rd.status_code,
            "corps": rd.text[:300],
            "fiche_supprimee": fiche,
        }


@workflows.workflow.define(
    name="zoho-delete",
    workflow_display_name="Zoho Delete",
    workflow_description="Suppression unitaire securisee (dry_run par defaut) d'un enregistrement Zoho par id.",
)
class ZohoDeleteWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: ZohoDeleteInput) -> dict:
        module = input.module.strip()
        return await supprimer_zoho(module=module, id_zoho=input.id_zoho, dry_run=input.dry_run)
