# Workflow "zoho-meta" — SylvaGreen Capital
# Récupère les métadonnées Zoho CRM (noms API des champs, valeurs de picklists,
# dispositions) pour les modules demandés (défaut : Leads, Deals).
# Secrets requis (Secrets Manager du workspace, liés au déploiement) :
#   - ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN
import os

import httpx
import mistralai.workflows as workflows
from pydantic import BaseModel

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"


class ZohoMetaInput(BaseModel):
    """Entrée du workflow : la liste des modules à décrire."""

    modules: list = ["Leads", "Deals"]


async def _access_token() -> str:
    params = {
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        "client_id": os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(ZOHO_ACCOUNTS, params=params)
        r.raise_for_status()
        return r.json()["access_token"]


@workflows.activity()
async def fetch_module_meta(module: str) -> dict:
    """Liste champs (api_name, label, type, picklist) et dispositions du module."""
    token = await _access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    async with httpx.AsyncClient(timeout=60) as client:
        rf = await client.get(
            f"{ZOHO_CRM}/settings/fields", params={"module": module}, headers=headers
        )
        if rf.status_code in (401, 403):
            # Fallback : lire un enregistrement pour lister au moins les noms API présents
            rr = await client.get(
                f"{ZOHO_CRM}/{module}", params={"per_page": 1}, headers=headers
            )
            rr.raise_for_status()
            data = rr.json().get("data", [])
            return {
                "module": module,
                "erreur_settings": f"HTTP {rf.status_code}",
                "champs_record": list(data[0].keys()) if data else [],
                "layouts": [],
            }
        rf.raise_for_status()
        fields = []
        for f in rf.json().get("fields", []):
            item = {
                "api_name": f.get("api_name"),
                "label": f.get("field_label"),
                "data_type": f.get("data_type"),
            }
            if f.get("pick_list_values"):
                item["picklist"] = [
                    {"valeur_api": v.get("actual_value"), "libelle": v.get("display_value")}
                    for v in f["pick_list_values"]
                ]
            fields.append(item)

        rl = await client.get(
            f"{ZOHO_CRM}/settings/layouts", params={"module": module}, headers=headers
        )
        layouts = []
        if rl.status_code == 200:
            layouts = [
                {
                    "id": l.get("id"),
                    "nom": l.get("name"),
                    "par_defaut": bool(l.get("is_default")),
                }
                for l in rl.json().get("layouts", [])
            ]
        return {"module": module, "fields": fields, "layouts": layouts}


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
