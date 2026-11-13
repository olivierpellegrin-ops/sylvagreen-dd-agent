"""Corrige les balises des comptes Atlas (format JSON correct)."""

import json
import os

import httpx

import mistralai.workflows as workflows

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"
TAGS = ["distributeurs", "Atlas 2019"]


@workflows.activity()
async def baliser_atlas() -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        params = {
            "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN", ""),
            "client_id": os.environ.get("ZOHO_CLIENT_ID", ""),
            "client_secret": os.environ.get("ZOHO_CLIENT_SECRET", ""),
            "grant_type": "refresh_token",
        }
        t = await client.post(ZOHO_ACCOUNTS, params=params)
        body = t.json()
        if "access_token" not in body:
            raise ValueError(f"Refresh token Zoho refuse : HTTP {t.status_code}")
        headers = {"Authorization": "Zoho-oauthtoken " + body["access_token"]}

        data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atlas_import_data.json")
        with open(data_path, encoding="utf-8") as fh:
            records = json.load(fh)
        noms = {(r["nom"] or "").strip().upper(): r["nom"] for r in records}

        comptes = []
        page = 1
        while page <= 12:
            rn = await client.get(
                f"{ZOHO_CRM}/Accounts",
                params={"fields": "Account_Name", "per_page": 200, "page": page},
                headers=headers,
            )
            if rn.status_code != 200:
                break
            data = rn.json().get("data", [])
            comptes.extend(data)
            if len(data) < 200:
                break
            page += 1

        cibles = [c for c in comptes if (c.get("Account_Name") or "").strip().upper() in noms]
        ok, errs = 0, []
        for c in cibles:
            rt = await client.post(
                f"{ZOHO_CRM}/Accounts/{c['id']}/actions/add_tags",
                json={"tags": [{"name": n} for n in TAGS]},
                headers=headers,
            )
            if rt.status_code in (200, 201):
                ok += 1
            else:
                errs.append({"id": c["id"], "nom": c.get("Account_Name"), "http": rt.status_code, "corps": rt.text[:200]})

        return {"cibles": len(cibles), "balises_ok": ok, "erreurs": errs[:8],
                "introuvables": [noms[n] for n in noms if n not in {(c.get("Account_Name") or "").strip().upper() for c in cibles}]}


@workflows.activity()
async def sonder_tag_distributeurs() -> dict:
    """Verifie que la recherche de comptes par balise distributeurs fonctionne."""
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
            raise ValueError(f"Refresh token refuse : HTTP {t.status_code}")
        headers = {"Authorization": "Zoho-oauthtoken " + body["access_token"]}
        rs = await client.get(
            f"{ZOHO_CRM}/Accounts/search",
            params={
                "criteria": "(Tag:equals:distributeurs)",
                "fields": "Account_Name,Billing_City,Billing_Latitude,Billing_Longitude",
                "per_page": 10,
            },
            headers=headers,
        )
        data = rs.json().get("data", []) if rs.status_code == 200 else []
        return {"status": rs.status_code, "nb": len(data),
                "corps": rs.text[:200] if rs.status_code != 200 else "",
                "sample": [{"nom": c.get("Account_Name"), "ville": c.get("Billing_City"),
                            "lat": c.get("Billing_Latitude")} for c in data[:5]]}
