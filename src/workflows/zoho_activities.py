"""Activites Zoho — I/O isolees hors du sandbox du workflow."""

import os
from typing import Optional

import httpx

import mistralai.workflows as workflows


ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"


@workflows.activity()
async def exchange_code(code: str, client_id: str, client_secret: str) -> dict:
    """Echange un code d'autorisation contre access_token + refresh_token (code a usage unique)."""
    params = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        t = await client.post(ZOHO_ACCOUNTS, params=params)
        body = t.json()
        if "access_token" not in body or "refresh_token" not in body:
            raise ValueError(
                f"Echange code Zoho refuse : HTTP {t.status_code} / {body}"
            )
        return {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "api_domain": body.get("api_domain"),
        }


def _picklists_des_champs(fields: list) -> list:
    """Extrait les picklists (valeur_api / libelle) d'une liste de champs settings/fields."""
    resultat = []
    for f in fields:
        if f.get("pick_list_values"):
            resultat.append(
                {
                    "api_name": f.get("api_name"),
                    "label": f.get("field_label"),
                    "picklist": [
                        {"valeur_api": v.get("actual_value"), "libelle": v.get("display_value")}
                        for v in f["pick_list_values"]
                    ],
                }
            )
    return resultat


@workflows.activity()
async def fetch_module_meta(module: str, access_token: Optional[str] = None) -> dict:
    """Liste champs (api_name, label, type, picklist) et dispositions du module Zoho.

    Si access_token est fourni, il est utilise tel quel (suite a un echange de code).
    Sinon, refresh token via les secrets d'environnement.
    Pour chaque disposition, les picklists specifiques sont aussi lues
    (settings/fields?layout_id=...) afin de detecter des valeurs renommees.
    Un echantillon d'enregistrements est inclus pour verifier les valeurs API reellement stockees.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        if not access_token:
            params = {
                "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN", ""),
                "client_id": os.environ.get("ZOHO_CLIENT_ID", ""),
                "client_secret": os.environ.get("ZOHO_CLIENT_SECRET", ""),
                "grant_type": "refresh_token",
            }
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
            access_token = body["access_token"]
        headers = {"Authorization": "Zoho-oauthtoken " + access_token}

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

        # Picklists specifiques a chaque disposition (valeurs potentiellement renommees)
        picklists_par_layout = []
        for l in layouts:
            rlf = await client.get(
                f"{ZOHO_CRM}/settings/fields",
                params={"module": module, "layout_id": l["id"]},
                headers=headers,
            )
            if rlf.status_code != 200:
                continue
            champs_layout = _picklists_des_champs(rlf.json().get("fields", []))
            if champs_layout:
                picklists_par_layout.append(
                    {"layout": l["nom"], "layout_id": l["id"], "champs_picklist": champs_layout}
                )

        # Echantillon d'enregistrements : valeurs API reellement stockees dans les fiches
        echantillons = []
        try:
            rs = await client.get(
                f"{ZOHO_CRM}/{module}", params={"per_page": 3, "page": 1}, headers=headers
            )
            if rs.status_code == 200:
                for rec in rs.json().get("data", []):
                    echantillons.append(
                        {k: rec.get(k) for k in ("id", "Full_Name", "Lead_Status", "Salutation") if rec.get(k) is not None}
                    )
        except Exception:
            pass

        return {
            "module": module,
            "fields": fields,
            "layouts": layouts,
            "picklists_par_layout": picklists_par_layout,
            "sample_records": echantillons,
        }
