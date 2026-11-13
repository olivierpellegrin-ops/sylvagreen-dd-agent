"""Activite de diagnostic Zoho Leads/search."""

import os

import httpx

import mistralai.workflows as workflows

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"


@workflows.activity()
async def tester_criteres(requete: str) -> dict:
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

        mots = [m for m in requete.replace("'", " ").split() if len(m) >= 3]
        mot1 = mots[0] if mots else ""
        mot2 = mots[-1] if len(mots) > 1 else ""
        or_chain = (
            f"((Last_Name:starts_with:{mot1})or(First_Name:starts_with:{mot1})"
            f"or(Company:starts_with:{mot1})or(Email:starts_with:{mot1}))"
        )

        tests: dict[str, str] = {
            "first_starts": f"(First_Name:starts_with:{mot1})",
            "last_starts": f"(Last_Name:starts_with:{mot2 or mot1})",
            "last_starts_avec_espace": f"(Last_Name:starts_with:{requete})",
            "or_chain": or_chain,
            "last_contains": f"(Last_Name:contains:{mot2 or mot1})",
            "full_name_starts": f"(Full_Name:starts_with:{mot1})",
            "full_name_contains": f"(Full_Name:contains:{mot2 or mot1})",
            "company_starts": f"(Company:starts_with:{mot2 or mot1})",
            "company_contains": f"(Company:contains:{mot2 or mot1})",
        }
        resultats = {}
        for nom, crit in tests.items():
            try:
                r = await client.get(
                    f"{ZOHO_CRM}/Leads/search",
                    params={"criteria": crit, "per_page": 5},
                    headers=headers,
                )
                if r.status_code == 204:
                    resultats[nom] = {"http": 204, "nb": 0, "criteria": crit}
                else:
                    corps = r.text[:400]
                    try:
                        data = r.json().get("data", [])
                    except Exception:
                        data = []
                    resultats[nom] = {
                        "http": r.status_code,
                        "criteria": crit,
                        "nb": len(data),
                        "fiches": [
                            {k: d.get(k) for k in ("id", "Full_Name", "Company", "Email") if d.get(k)}
                            for d in data[:5]
                        ],
                        "corps_brut": corps if r.status_code != 200 else "",
                    }
            except Exception as e:
                resultats[nom] = {"criteria": crit, "erreur": str(e)[:300]}
        return {"requete": requete, "mots": mots, "resultats": resultats}
