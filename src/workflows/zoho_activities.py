"""Activites Zoho — I/O isolees hors du sandbox du workflow."""

import os

import httpx

import mistralai.workflows as workflows


ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"


@workflows.activity()
async def fetch_module_meta(module: str) -> dict:
    """Liste champs (api_name, label, type, picklist) et dispositions du module Zoho."""
    params = {
        "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN", ""),
        "client_id": os.environ.get("ZOHO_CLIENT_ID", ""),
        "client_secret": os.environ.get("ZOHO_CLIENT_SECRET", ""),
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        t = await client.post(ZOHO_ACCOUNTS, params=params)
        body = t.json()
        if "access_token" not in body:
            raise ValueError(
                "Echange refresh token Zoho refuse : "
                f"HTTP {t.status_code} / {body} / "
                f"secrets presents : RT={bool(params['refresh_token'])}, "
                f"CID={bool(params['client_id'])}, "
                f"CS={bool(params['client_secret'])}"
            )
        headers = {"Authorization": "Zoho-oauthtoken " + body["access_token"]}

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
