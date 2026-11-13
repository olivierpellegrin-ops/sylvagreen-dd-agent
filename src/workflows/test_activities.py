"""Activites de diagnostic : recherche Zoho, rendu WeasyPrint, sondage geo."""

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
        or_chain = (
            f"((Last_Name:starts_with:{mot1})or(First_Name:starts_with:{mot1})"
            f"or(Company:starts_with:{mot1})or(Email:starts_with:{mot1}))"
        )
        tests: dict = {
            "or_chain": or_chain,
            "company_starts": f"(Company:starts_with:{mots[-1] if len(mots) > 1 else mot1})",
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
                    try:
                        data = r.json().get("data", [])
                    except Exception:
                        data = []
                    resultats[nom] = {"http": r.status_code, "nb": len(data),
                                      "corps_brut": r.text[:300] if r.status_code != 200 else ""}
            except Exception as e:
                resultats[nom] = {"criteria": crit, "erreur": str(e)[:300]}
        return {"requete": requete, "resultats": resultats}


@workflows.activity()
async def tester_weasy(variante: str = "societe") -> dict:
    """Rend le gabarit d'etude (logo + tokens remplis, contenu minimal) en PDF."""
    import traceback

    import weasyprint

    base = "/app/agent/etudes"
    gabarit = {"societe": "template_societe.html", "particulier": "template_particulier.html",
               "particulier_transmission": "template_particulier_transmission.html"}.get(variante, "template_societe.html")
    chemin = os.path.join(base, gabarit)
    if not os.path.exists(chemin):
        return {"erreur": f"gabarit introuvable : {chemin}", "listing": os.listdir("/app")[:30]}
    html = open(chemin, encoding="utf-8").read()
    logo = "".join(open(os.path.join(base, "logo_base64.txt"), encoding="utf-8").read().split())
    html = html.replace("{{LOGO}}", "data:image/png;base64," + logo)
    html = html.replace("{{DATE}}", "13/11/2026").replace("{{DESTINATAIRE}}", "DE SLOOVERE L")
    html = html.replace("{{COUV_DESTINATAIRE}}", "<p>DE SLOOVERE L</p><p>1068 rue des Fèves</p><p>59226 Lecelles</p>")
    for n in range(1, 7):
        token = "{{PAGE%d_BODY}}" % n
        if token in html:
            html = html.replace(token, "<h2>%d. Chapitre de test</h2><p>Contenu minimal de test.</p>" % n)
    try:
        pdf = weasyprint.HTML(string=html).write_pdf()
        return {"weasyprint": weasyprint.__version__, "gabarit": gabarit,
                "nb_h2": html.count("<h2>"), "taille_pdf": len(pdf), "succes": True}
    except Exception as e:
        return {"weasyprint": weasyprint.__version__, "succes": False,
                "erreur": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-600:]}


@workflows.activity()
async def sonder_geo(ville: str = "Lecelles") -> dict:
    """Sonde : 1) champs GPS des Leads ; 2) comptes geolocalises par industrie ;
    3) geocodage de la ville cible (api-adresse.data.gouv.fr)."""
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
            raise ValueError(f"Token Zoho refuse : HTTP {t.status_code} / {body}")
        headers = {"Authorization": "Zoho-oauthtoken " + body["access_token"]}

        rf = await client.get(f"{ZOHO_CRM}/settings/fields", params={"module": "Leads"}, headers=headers)
        champs = rf.json().get("fields", []) if rf.status_code == 200 else []
        gps_leads = [c.get("api_name") for c in champs
                     if any(k in (c.get("api_name") or "").lower() for k in ("latitude", "longitude", "gps"))]

        comptes = []
        page = 1
        while page <= 3:
            rc = await client.get(
                f"{ZOHO_CRM}/Accounts",
                params={
                    "fields": "Account_Name,Industry,Billing_Latitude,Billing_Longitude,Billing_City,Website",
                    "per_page": 200, "page": page,
                },
                headers=headers,
            )
            if rc.status_code != 200:
                break
            data = rc.json().get("data", [])
            comptes.extend(data)
            if len(data) < 200:
                break
            page += 1

        par_industrie = {}
        for c in comptes:
            ind = c.get("Industry") or "(vide)"
            lat, lon = c.get("Billing_Latitude"), c.get("Billing_Longitude")
            entry = par_industrie.setdefault(ind, {"nb": 0, "geolocalises": 0})
            entry["nb"] += 1
            try:
                if lat not in (None, "", 0) and float(lat) != 0 and lon not in (None, "", 0):
                    entry["geolocalises"] += 1
            except (TypeError, ValueError):
                pass

        rg = await client.get("https://api-adresse.data.gouv.fr/search/", params={"q": ville, "limit": 1})
        geo = {}
        if rg.status_code == 200 and rg.json().get("features"):
            f = rg.json()["features"][0]
            geo = {"label": f["properties"]["label"],
                   "lat": f["geometry"]["coordinates"][1],
                   "lon": f["geometry"]["coordinates"][0]}

        return {"gps_champs_leads": gps_leads, "nb_comptes_lus": len(comptes),
                "par_industrie": par_industrie, "geo_ville": geo}
