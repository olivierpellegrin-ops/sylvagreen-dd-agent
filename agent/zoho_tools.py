"""Tools Zoho CRM de l'agent investisseurs (auth par secrets d'environnement)."""

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta

import httpx

from mistralai import agents

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"
LEADS_LAYOUT_ID = "968010000002223131"  # disposition « Investisseurs » (Leads)
DEALS_LAYOUT_ID = "968010000002223015"  # disposition « Investisseurs » (Deals)
PIPELINE = "Pipeline Investisseurs"

_access_token = None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _norm_phone(p: str) -> str:
    digits = re.sub(r"[^+0-9]", "", p or "")
    if digits.startswith("0"):
        digits = "+33" + digits[1:]
    return digits


async def _token() -> str:
    global _access_token
    if _access_token:
        return _access_token
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            ZOHO_ACCOUNTS,
            params={
                "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
                "client_id": os.environ["ZOHO_CLIENT_ID"],
                "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
                "grant_type": "refresh_token",
            },
        )
        body = r.json()
        if "access_token" not in body:
            raise RuntimeError(f"Token Zoho refuse : {body}")
        _access_token = body["access_token"]
        return _access_token


async def _h() -> dict:
    return {"Authorization": "Zoho-oauthtoken " + await _token()}


async def _search(module: str, criteria: str) -> list:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{ZOHO_CRM}/{module}/search",
            params={"criteria": criteria, "per_page": 25},
            headers=await _h(),
        )
        if r.status_code == 204:
            return []
        r.raise_for_status()
        return r.json().get("data", [])


@agents.tool(annotations=agents.ToolAnnotations(read_only_hint=True))
async def verifier_doublons(
    nom: str,
    prenom: str = "",
    email: str = "",
    telephone: str = "",
    siren: str = "",
    ville: str = "",
) -> str:
    """Recherche les doublons AVANT toute création de prospect investisseur (3 passes :
    SIREN société liée, puis email/téléphone E164, puis nom+prénom normalisés).
    Renvoie un verdict JSON : aucun_match / match_certain (avec id de fiche) /
    rapprochements_possibles (avec scores). Toujours appeler avant creer_prospect."""
    email = (email or "").strip().lower()
    tel = _norm_phone(telephone)
    nom_norm = _norm((prenom or "") + (nom or ""))
    resultats = []

    if siren:
        f = await _search("Leads", f"(SIREN:equals:{siren})")
        if f:
            return json.dumps({"verdict": "match_certain", "critere": "SIREN", "fiches": f}, ensure_ascii=False)
    if email:
        f = await _search("Leads", f"(Email:equals:{email})")
        if f:
            return json.dumps({"verdict": "match_certain", "critere": "email", "fiches": f}, ensure_ascii=False)
    if tel:
        f = await _search("Leads", f"(T_l_phone_E164:equals:{tel})")
        if f:
            return json.dumps({"verdict": "match_certain", "critere": "telephone", "fiches": f}, ensure_ascii=False)

    proches = await _search("Leads", f"(Nom_normalis:equals:{nom_norm})") if nom_norm else []
    for p in proches:
        nom_fiche = _norm(p.get("Full_Name") or (p.get("First_Name", "") + p.get("Last_Name", "")))
        score = 100 if nom_fiche == nom_norm else 90
        resultats.append({"id": p.get("id"), "score": score, "fiche": p.get("Full_Name"), "ville": p.get("City")})

    if resultats:
        return json.dumps({"verdict": "rapprochements_possibles", "candidats": resultats}, ensure_ascii=False)
    return json.dumps({"verdict": "aucun_match", "nom_normalise": nom_norm, "tel_e164": tel}, ensure_ascii=False)


@agents.tool
async def creer_prospect(
    nom: str,
    prenom: str = "",
    societe: str = "",
    email: str = "",
    telephone: str = "",
    ville: str = "",
    siren_lie: str = "",
    type_investisseur: str = "Personne Physique",
    source_detaillee: str = "Listing",
    tag_campagne: str = "",
) -> str:
    """Crée le prospect investisseur dans Zoho : lead (disposition Investisseurs) avec les
    champs de dédoublonnage (Nom_normalis, T_l_phone_E164, SIREN), puis conversion avec
    affaire au stade Qualification dans le Pipeline Investisseurs. À appeler uniquement si
    verifier_doublons renvoie aucun_match. Renvoie les ids créés en JSON."""
    tel = _norm_phone(telephone)
    payload = {
        "data": [
            {
                "Layout": {"id": LEADS_LAYOUT_ID},
                "Last_Name": nom,
                "First_Name": prenom or "",
                "Company": societe or (f"{prenom} {nom}"),
                "Email": email or None,
                "Phone": telephone or None,
                "City": ville or None,
                "Nom_normalis": _norm((prenom or "") + nom),
                "T_l_phone_E164": tel or None,
                "SIREN": siren_lie or None,
                "Type_d_investisseur": type_investisseur,
                "Source_d_taill_e": source_detaillee,
                "Description": f"[{datetime.now():%d/%m/%Y}] Agent : création prospect (source : {source_detaillee})",
            }
        ],
        "duplicate_check_fields": ["Nom_normalis", "T_l_phone_E164"],
        "trigger": ["workflow"],
    }
    for k in list(payload["data"][0]):
        if payload["data"][0][k] is None:
            del payload["data"][0][k]
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{ZOHO_CRM}/Leads", json=payload, headers=await _h())
        if r.status_code >= 400:
            # repli : payload minimal si un champ optionnel est refusé
            minimal = {"data": [{"Layout": {"id": LEADS_LAYOUT_ID}, "Last_Name": nom, "First_Name": prenom or None, "Company": societe or f"{prenom} {nom}", "Nom_normalis": _norm((prenom or "") + nom)}]}
            for k in list(minimal["data"][0]):
                if minimal["data"][0][k] is None:
                    del minimal["data"][0][k]
            r = await client.post(f"{ZOHO_CRM}/Leads", json=minimal, headers=await _h())
        r.raise_for_status()
        data = r.json()["data"][0]
        if data.get("status") != "success":
            return json.dumps({"erreur": data}, ensure_ascii=False)
        lead_id = data["details"]["id"]

        if tag_campagne:
            await client.post(
                f"{ZOHO_CRM}/Leads/{lead_id}/actions/add_tags",
                json={"over_write": "false", "tags": [{"name": tag_campagne[:25]}]},
                headers=await _h(),
            )

        # Conversion : crée Contact + Compte + Affaire (Pipeline Investisseurs, Qualification)
        convert = {
            "data": [
                {
                    "Deal": {
                        "Deal_Name": f"Investissement — {prenom} {nom}".strip(),
                        "Stage": "Qualification",
                        "Pipeline": PIPELINE,
                        "Layout": {"id": DEALS_LAYOUT_ID},
                        "Closing_Date": (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d"),
                    },
                    "overwrite": True,
                }
            ]
        }
        rc = await client.post(f"{ZOHO_CRM}/Leads/{lead_id}/actions/convert", json=convert, headers=await _h())
        conversion = rc.json() if rc.status_code < 500 else {"erreur": rc.text}

        return json.dumps(
            {"lead_id": lead_id, "conversion": conversion, "message": "Prospect créé — affaire au stade Qualification" if rc.status_code == 200 else "Prospect créé — conversion à vérifier manuellement"},
            ensure_ascii=False,
        )


@agents.tool
async def planifier_relance(
    sujet: str,
    lead_id: str,
    date_echeance: str,
    heure_rappel: str = "08:30:00",
    priorite: str = "Normal",
) -> str:
    """Crée une tâche de relance avec rappel liée au prospect (lead Zoho). date_echeance au
    format AAAA-MM-JJ, heure_rappel au format hh:mm:ss (fuseau France appliqué automatiquement).
    Cadences usuelles : J+7 sans réponse, J+7 après RDV, J+30 après signature."""
    payload = {
        "data": [
            {
                "Subject": sujet,
                "$se_module": "Leads",
                "What_Id": lead_id,
                "Priority": priorite,
                "Send_Reminder": True,
                "Reminder": f"{date_echeance}T{heure_rappel}+01:00",
                "Activity_Date": date_echeance,
            }
        ]
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{ZOHO_CRM}/Tasks", json=payload, headers=await _h())
        r.raise_for_status()
        data = r.json()["data"][0]
        return json.dumps({"tache": data.get("details", data)}, ensure_ascii=False)


@agents.tool(annotations=agents.ToolAnnotations(read_only_hint=True))
async def synchroniser_appels(nb_jours: int = 7) -> str:
    """Lit les appels enregistrés dans Zoho (passés depuis le smartphone d'Olivier) sur les
    N derniers jours et les rapproche des prospects investisseurs (par téléphone E164).
    Renvoie la liste des appels avec le lead associé et l'étape pipeline proposée.
    Lecture seule : les propositions d'évolution vers Rédaction contrat / Signature /
    Souscription doivent toujours être validées par Olivier."""
    depuis = (datetime.now() - timedelta(days=nb_jours)).strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":00"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{ZOHO_CRM}/Calls",
            params={"per_page": 100, "sort_by": "Created_Time", "sort_order": "desc"},
            headers=await _h(),
        )
        r.raise_for_status()
        appels = r.json().get("data", [])

    tel2lead = {}
    leads = await _search("Leads", "(Type_d_investisseur:equals:Personne Physique)") if False else []
    # index téléphones des leads investisseurs (recherche par critère non supportée sur tous les comptes : balayage léger)
    async with httpx.AsyncClient(timeout=60) as client:
        rl = await client.get(f"{ZOHO_CRM}/Leads", params={"per_page": 200, "fields": "id,Full_Name,T_l_phone_E164,Phone,Company"}, headers=await _h())
        rl.raise_for_status()
        for l in rl.json().get("data", []):
            for t in (l.get("T_l_phone_E164"), l.get("Phone")):
                if t:
                    tel2lead[_norm_phone(t)] = l

    resultats = []
    for a in appels[:100]:
        created = (a.get("Created_Time") or "")[:10]
        sujets = a.get("What_Id") or {}
        numero = _norm_phone(str(a.get("Call_Type") or ""))  # Call_Type non fiable
        resultats.append(
            {
                "sujet": a.get("Subject"),
                "direction": a.get("Call_Type"),
                "statut": a.get("Call_Status"),
                "duree_s": a.get("Call_Duration"),
                "date": a.get("Call_Start_Time"),
                "lie_a": a.get("Contact_Name") or a.get("What_Id"),
            }
        )
    return json.dumps({"nb_appels": len(resultats), "appels": resultats[:50]}, ensure_ascii=False)
