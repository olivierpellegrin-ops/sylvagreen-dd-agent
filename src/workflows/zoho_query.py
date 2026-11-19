"""Workflow zoho-query : lecture generique et securisee du CRM Zoho (recherche par
critere ou par mot, sur n'importe quel module). Retourne les enregistrements
correspondants. Lecture seule : aucune ecriture dans le CRM.
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

# champs de "nom" par module pour la recherche par mot (starts_with)
CHAMPS_NOM = {
    "Accounts": ["Account_Name"],
    "Leads": ["Last_Name", "First_Name", "Company"],
    "Contacts": ["Last_Name", "First_Name", "Account_Name"],
    "Deals": ["Deal_Name"],
    "Tasks": ["Subject"],
    "Calls": ["Subject", "Call_Details"],
}

CHAMPS_DEFAUT = {
    "Accounts": (
        "Account_Name,Account_Number,Account_Site,Industry,SIRET,Phone,Website,"
        "Billing_Street,Billing_City,Billing_Zip_Code,Billing_State,"
        "Billing_Latitude,Billing_Longitude,Parent_Account,"
        "Created_Time,Modified_Time,Description,Tag"
    ),
    "Leads": (
        "Salutation,First_Name,Last_Name,Company,Lead_Status,SIREN,Phone,Mobile,Email,"
        "Street,City,Zip_Code,State,Lead_Source,Created_Time,Modified_Time,Description,Tag"
    ),
    "Contacts": (
        "Salutation,First_Name,Last_Name,Account_Name,Designation,Email,Phone,Mobile,"
        "Mailing_Street,Mailing_City,Mailing_Zip,Mailing_State,Created_Time,Modified_Time,Description,Tag"
    ),
    "Deals": (
        "Deal_Name,Stage,Pipeline,Type,Amount,Account_Name,Contact_Name,"
        "Closing_Date,Created_Time,Modified_Time,Description,Tag"
    ),
    "Tasks": "Subject,Status,Priority,Due_Date,What_Id,Created_Time,Description",
    "Calls": "Subject,Call_Type,Call_Start_Time,What_Id,Who_Id,Description",
}


class ZohoQueryInput(BaseModel):
    """Une requete : soit un criteria Zoho complet, soit des mots (starts_with).

    module : Accounts (defaut), Leads, Contacts, Deals, Tasks, Calls...
    criteria : ex. "(SIRET:equals:123456789)" ou "(Account_Name:starts_with:Lesbats)"
    requete : mots separes par des espaces, cherches par starts_with sur les
              champs de nom du module (non cumulable avec criteria).
    champs : liste de champs API a retourner (defaut selon module).
    limite : nombre max d'enregistrements (defaut 20).
    """

    module: str = "Accounts"
    criteria: str | None = None
    requete: str | None = None
    champs: str | None = None
    limite: int = 20


@workflows.activity()
async def lire_zoho(module: str, criteria: str, champs: str, limite: int) -> dict:
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
        r = await client.get(
            f"{ZOHO_CRM}/{module}/search",
            params={"criteria": criteria, "fields": champs, "per_page": min(limite, 100)},
            headers=headers,
        )
        if r.status_code == 204:
            return {"http": 204, "nb": 0, "data": []}
        if r.status_code != 200:
            return {"http": r.status_code, "nb": 0, "data": [], "erreur": r.text[:400]}
        data = r.json().get("data", [])
        return {"http": 200, "nb": len(data), "data": data[:limite]}


@workflows.workflow.define(
    name="zoho-query",
    workflow_display_name="Zoho Query",
    workflow_description="Recherche generique en lecture seule dans le CRM Zoho (comptes, prospects, contacts, affaires...).",
)
class ZohoQueryWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: ZohoQueryInput) -> dict:
        module = input.module.strip()
        champs = input.champs or CHAMPS_DEFAUT.get(module, "id,Created_Time,Modified_Time")
        limite = max(1, min(input.limite, 100))
        if input.criteria:
            criteria = input.criteria.strip()
        elif input.requete:
            champs_nom = CHAMPS_NOM.get(module)
            if not champs_nom:
                return {"erreur": f"Recherche par mot non supportee pour le module {module} ; utiliser criteria."}
            mots = [m for m in input.requete.replace("'", " ").split() if len(m) >= 2]
            if not mots:
                return {"erreur": "requete vide"}
            ors = []
            for m in mots:
                for c in champs_nom:
                    ors.append(f"({c}:starts_with:{m})")
            criteria = "(" + "or".join(ors) + ")"
        else:
            return {"erreur": "Fournir criteria ou requete."}
        resultat = await lire_zoho(module=module, criteria=criteria, champs=champs, limite=limite)
        return {"module": module, "criteria": criteria, "champs": champs, **resultat}
