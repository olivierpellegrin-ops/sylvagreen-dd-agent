"""Mass-update Industry des comptes Atlas vers la nouvelle valeur picklist."""

import os
import re

import httpx

import mistralai.workflows as workflows

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"


@workflows.activity()
async def maj_industry_atlas() -> dict:
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
            raise ValueError(f"Refresh token refuse : HTTP {t.status_code}")
        headers = {"Authorization": "Zoho-oauthtoken " + body["access_token"]}

        # 1) valeur reelle de la picklist Industry cote Zoho
        rf = await client.get(f"{ZOHO_CRM}/settings/fields", params={"module": "Accounts"}, headers=headers)
        if rf.status_code != 200:
            return {"erreur": f"settings/fields HTTP {rf.status_code}"}
        valeurs = []
        for f in rf.json().get("fields", []):
            if f.get("api_name") == "Industry":
                valeurs = [(v.get("actual_value"), v.get("display_value")) for v in (f.get("pick_list_values") or [])]
                break
        cible = next((a for a, _ in valeurs if re.search(r"distributeur", str(a), re.I) and re.search(r"pellet", str(a), re.I)), None)
        if not cible:
            return {"erreur": "valeur distributeur pellets introuvable dans la picklist", "valeurs": valeurs}

        # 2) ids des comptes balises distributeurs
        ids = []
        page = 1
        while page <= 5:
            rs = await client.get(
                f"{ZOHO_CRM}/Accounts/search",
                params={"criteria": "(Tag:equals:distributeurs)", "fields": "Account_Name", "per_page": 200, "page": page},
                headers=headers,
            )
            if rs.status_code != 200:
                break
            data = rs.json().get("data", [])
            ids.extend(c["id"] for c in data)
            if len(data) < 200:
                break
            page += 1

        # 3) PUT par lots de 100
        ok, errs = 0, []
        for i in range(0, len(ids), 100):
            lot = ids[i:i + 100]
            payload = {"data": [{"id": cid, "Industry": cible} for cid in lot]}
            ru = await client.put(f"{ZOHO_CRM}/Accounts", json=payload, headers=headers)
            if ru.status_code in (200, 201):
                for d in ru.json().get("data", []):
                    if d.get("code") in ("SUCCESS", "200"):
                        ok += 1
                    else:
                        errs.append(d)
            else:
                errs.append({"http": ru.status_code, "corps": ru.text[:200]})

        return {"industry_appliquee": cible, "valeurs_picklist": valeurs,
                "comptes_vises": len(ids), "mises_a_jour_ok": ok, "erreurs": errs[:5], "nb_erreurs": len(errs)}
