"""Activite d'import des comptes Atlas distributeurs (sans contacts)."""

import json
import os

import httpx

import mistralai.workflows as workflows

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"
TAGS = ["distributeurs", "Atlas 2019"]
INDUSTRY = "Distributeurs pellets"


@workflows.activity()
async def importer_atlas(dry_run: bool = False) -> dict:
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

        # 1) le champ SIREN existe-t-il sur Accounts ?
        champs_api = []
        rf = await client.get(f"{ZOHO_CRM}/settings/fields", params={"module": "Accounts"}, headers=headers)
        if rf.status_code == 200:
            champs_api = [f.get("api_name") for f in rf.json().get("fields", [])]
        a_siren = "SIREN" in champs_api

        # 2) Industry picklist : « Distributeurs pellets » est-il autorise ?
        industry_ok = True
        if rf.status_code == 200:
            for f in rf.json().get("fields", []):
                if f.get("api_name") == "Industry":
                    vals = [(v.get("actual_value") or "") for v in (f.get("pick_list_values") or [])]
                    industry_ok = INDUSTRY in vals
                    break

        # 3) noms de comptes existants (anti-doublon)
        existants = set()
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
            existants.update((c.get("Account_Name") or "").strip().upper() for c in data)
            if len(data) < 200:
                break
            page += 1

        def norm_nom(s):
            return (s or "").strip().upper()

        a_creer = [r for r in records if norm_nom(r["nom"]) not in existants]
        sautes = [r["nom"] for r in records if norm_nom(r["nom"]) in existants]

        if dry_run:
            return {"dry_run": True, "total": len(records), "a_creer": len(a_creer),
                    "doublons_existants": len(sautes), "doublons_noms": sautes,
                    "champ_siren_comptes": a_siren, "industry_autorisee": industry_ok}

        # 4) creation par lots de 100
        crees, erreurs = [], []
        for i in range(0, len(a_creer), 100):
            lot = a_creer[i:i + 100]
            payload = {"data": []}
            for r in lot:
                ca_txt = "{:,.0f}".format(r["ca"]).replace(",", " ") + " €" if r.get("ca") else "n/a"
                res_txt = "{:,.0f}".format(r["resultat"]).replace(",", " ") + " €" if r.get("resultat") else "n/a"
                fiab = ("Bonne" if r["source"] == "atlas_bonne"
                        else "À vérifier confirmée (registre : " + (r.get("nom_registre") or "") + ")")
                desc = ("Importé de l'Atlas des distributeurs de granulés en vrac 2019 (Vibe, 13/11/2026). "
                        "SIREN : " + (r.get("siren") or "n/a") + ". "
                        "NAF : " + (r.get("naf") or "n/a") + ". "
                        "Effectif : " + (r.get("effectif") or "n/a") + ". "
                        "CA dernier exercice (" + (r.get("annee_ca") or "?") + ") : " + ca_txt + ". "
                        "Résultat net : " + res_txt + ". "
                        "Fiabilité rapprochement : " + fiab + ". "
                        "Sans contact (choix Olivier).")
                item = {
                    "Account_Name": r["nom"],
                    "Website": r.get("site_web") or None,
                    "Billing_Street": r.get("rue") or None,
                    "Billing_Zip": r.get("cp") or None,
                    "Billing_City": r.get("ville") or None,
                    "Billing_State": r.get("region") or None,
                    "Billing_Country": "France",
                    "Billing_Latitude": str(r["lat"]) if r.get("lat") else None,
                    "Billing_Longitude": str(r["lon"]) if r.get("lon") else None,
                    "Annual_Revenue": r.get("ca"),
                    "Description": desc,
                }
                if industry_ok:
                    item["Industry"] = INDUSTRY
                if a_siren:
                    item["SIREN"] = r.get("siren")
                payload["data"].append({k: v for k, v in item.items() if v is not None})
            rc = await client.post(f"{ZOHO_CRM}/Accounts", json=payload, headers=headers)
            if rc.status_code in (200, 201):
                for d in rc.json().get("data", []):
                    if d.get("code") in ("SUCCESS", "201"):
                        crees.append({"id": d.get("details", {}).get("id"), "nom": d.get("details", {}).get("Account_Name")})
                    else:
                        erreurs.append(d)
            else:
                erreurs.append({"http": rc.status_code, "corps": rc.text[:300]})

        # 5) balises
        tags_ok, tags_err = 0, []
        for c in crees:
            if not c.get("id"):
                continue
            rt = await client.post(
                f"{ZOHO_CRM}/Accounts/{c['id']}/actions/add_tags",
                params={"names": ",".join(TAGS)},
                headers=headers,
            )
            if rt.status_code in (200, 201):
                tags_ok += 1
            else:
                tags_err.append({"id": c["id"], "http": rt.status_code, "corps": rt.text[:150]})

        return {
            "dry_run": False,
            "total_fichier": len(records),
            "doublons_saufes": len(sautes),
            "doublons_noms": sautes,
            "crees": len(crees),
            "erreurs_creation": erreurs[:10],
            "nb_erreurs": len(erreurs),
            "champ_siren_comptes": a_siren,
            "industry_utilisee": INDUSTRY if industry_ok else "(absente : picklist sans cette valeur)",
            "industry_autorisee": industry_ok,
            "tags_ok": tags_ok,
            "tags_erreurs": tags_err[:5],
        }
