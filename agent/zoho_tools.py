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
_picklist_cache: dict = {}


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


async def _picklist(client, module: str, champ_api: str, layout_id: str = "") -> list:
    """Renvoie [(valeur_api, libelle_affiche)] du champ picklist, lus dans les métadonnées
    de l'organisation (endpoint settings/fields), en priorité pour la disposition donnée.
    Aucune valeur n'est codée en dur : source de vérité = le CRM lui-même."""
    cle = (module, champ_api, layout_id)
    if cle in _picklist_cache:
        return _picklist_cache[cle]
    params = {"module": module}
    if layout_id:
        params["layout_id"] = layout_id
    r = await client.get(f"{ZOHO_CRM}/settings/fields", params=params, headers=await _h())
    valeurs: list = []
    if r.status_code == 200:
        for f in r.json().get("fields", []):
            if f.get("api_name") == champ_api:
                valeurs = [
                    (v.get("actual_value"), v.get("display_value"))
                    for v in (f.get("pick_list_values") or [])
                ]
                break
    if layout_id and not valeurs:
        # disposition sans valeurs spécifiques ou paramètre refusé : repli module entier
        return await _picklist(client, module, champ_api, "")
    if valeurs:
        _picklist_cache[cle] = valeurs
    return valeurs


async def _resolve(client, saisie: str, module: str, champ_api: str, layout_id: str = ""):
    """Traduit une saisie utilisateur (libellé français affiché OU valeur API) en valeur
    API valide du picklist, en comparant aux métadonnées réelles du CRM (normalisation
    accents/casse/ponctuation). Renvoie (valeur_api ou None, valeurs_valides)."""
    valeurs = await _picklist(client, module, champ_api, layout_id)
    n = _norm(saisie)
    if n:
        for actual, libelle in valeurs:
            if n in (_norm(actual), _norm(libelle)):
                return actual, valeurs
    return None, valeurs


@agents.tool(annotations=agents.ToolAnnotations(read_only_hint=True))
async def lister_valeurs(module: str = "Leads", champ: str = "Lead_Status", disposition_investisseurs: bool = True) -> str:
    """Liste les valeurs valides d'un champ liste déroulante Zoho (ex. champ Lead_Status
    du module Leads, Salutation, Type_d_investisseur...), pour la disposition
    Investisseurs (par défaut) ou la disposition standard du module. Renvoie pour chaque
    valeur son libellé affiché dans Zoho et sa valeur API. À utiliser avant maj_prospect
    en cas de doute ou d'erreur sur un statut ou une civilité, et pour répondre aux
    questions d'Olivier sur les valeurs disponibles. Lecture seule."""
    layout = LEADS_LAYOUT_ID if (disposition_investisseurs and module == "Leads") else ""
    async with httpx.AsyncClient(timeout=60) as client:
        valeurs = await _picklist(client, module, champ, layout)
    return json.dumps(
        {"module": module, "champ": champ, "valeurs": [{"libelle": d, "valeur_api": a} for a, d in valeurs]},
        ensure_ascii=False,
    )


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


_SALUTATIONS = {
    "m": "Mr.", "m.": "Mr.", "mr": "Mr.", "mr.": "Mr.", "monsieur": "Mr.",
    "mme": "Mrs.", "mme.": "Mrs.", "mrs": "Mrs.", "mrs.": "Mrs.", "madame": "Mrs.",
    "mlle": "Ms.", "mlle.": "Ms.", "ms": "Ms.", "ms.": "Ms.", "mademoiselle": "Ms.",
    "dr": "Dr.", "dr.": "Dr.", "docteur": "Dr.",
    "pr": "Prof.", "prof.": "Prof.", "professeur": "Prof.",
}

# Repli de secours si les métadonnées du CRM sont inaccessibles.
_STATUTS_SECOURS = {
    "acontacterdanslefutur": "Contact in Future", "contactinfuture": "Contact in Future",
    "tentativedecontact": "Attempted to Contact", "attemptedtocontact": "Attempted to Contact",
    "contacte": "Contacted", "contacted": "Contacted",
    "noncontacte": "Not Contacted", "notcontacted": "Not Contacted",
    "prequalifie": "Pre-Qualified", "prequalified": "Pre-Qualified",
    "nonqualifie": "Not Qualified", "notqualified": "Not Qualified",
    "prospectindesirable": "Junk Lead", "junklead": "Junk Lead", "indesirable": "Junk Lead",
    "prospectperdu": "Lost Lead", "lostlead": "Lost Lead", "perdu": "Lost Lead",
}


def _salutation(civilite: str) -> str:
    """Convertit une civilité française (M., Mme, Mlle...) en valeur API du picklist
    Salutation de Zoho (Mr., Mrs., Ms., Dr., Prof.). Vide si inconnue. Uniquement
    utilisé comme repli : la résolution normale passe par les métadonnées du CRM."""
    return _SALUTATIONS.get((civilite or "").strip().lower(), "")


@agents.tool
async def maj_prospect(
    lead_id: str,
    civilite: str = "",
    statut_prospect: str = "",
    note: str = "",
    email: str = "",
    telephone: str = "",
    ville: str = "",
    type_investisseur: str = "",
    source_detaillee: str = "",
) -> str:
    """Met à jour un lead Zoho EXISTANT (ne crée jamais de fiche). Identifie la fiche par
    son lead_id. Champs modifiables (seuls ceux fournis sont changés) : Civilité
    (civilite : M./Mme/Mlle...), Status du Prospect (statut_prospect : donner le libellé
    affiché dans Zoho, ex. "A contacter dans le futur" — la valeur API valide est résolue
    automatiquement depuis les métadonnées du CRM), email, telephone, ville, type
    d'investisseur, source détaillée. note : texte ajouté en fin de Description au format
    [JJ/MM/AAAA] Agent : note. Renvoie JSON {lead_id, modifie: [champs]}."""
    async with httpx.AsyncClient(timeout=60) as client:
        champs: dict = {"id": lead_id}
        modifies = []
        if civilite:
            salut, _ = await _resolve(client, civilite, "Leads", "Salutation", LEADS_LAYOUT_ID)
            salut = salut or _salutation(civilite)
            if salut:
                champs["Salutation"] = salut
                modifies.append("Civilité")
        if statut_prospect:
            api, valeurs = await _resolve(client, statut_prospect, "Leads", "Lead_Status", LEADS_LAYOUT_ID)
            if not api:
                api = _STATUTS_SECOURS.get(_norm(statut_prospect), "")
            if not api:
                libelles = "; ".join(f"{lib} (= {act})" for act, lib in valeurs) or "interroger lister_valeurs"
                return json.dumps(
                    {"erreur": f"Statut inconnu : {statut_prospect}. Valeurs acceptées : {libelles}"},
                    ensure_ascii=False,
                )
            champs["Lead_Status"] = api
            modifies.append("Status du Prospect")
        if email:
            champs["Email"] = email.strip().lower()
            modifies.append("Email")
        if telephone:
            champs["Phone"] = telephone
            champs["T_l_phone_E164"] = _norm_phone(telephone)
            modifies.append("Téléphone")
        if ville:
            champs["City"] = ville
            modifies.append("Ville")
        if type_investisseur:
            api, valeurs = await _resolve(client, type_investisseur, "Leads", "Type_d_investisseur", LEADS_LAYOUT_ID)
            if api:
                champs["Type_d_investisseur"] = api
            elif valeurs:
                libelles = "; ".join(f"{lib} (= {act})" for act, lib in valeurs)
                return json.dumps({"erreur": f"Type d'investisseur inconnu : {type_investisseur}. Valeurs acceptées : {libelles}"}, ensure_ascii=False)
            else:
                champs["Type_d_investisseur"] = type_investisseur
            modifies.append("Type d'investisseur")
        if source_detaillee:
            api, _ = await _resolve(client, source_detaillee, "Leads", "Source_d_taill_e", LEADS_LAYOUT_ID)
            champs["Source_d_taill_e"] = api or source_detaillee
            modifies.append("Source détaillée")
        if note:
            r = await client.get(f"{ZOHO_CRM}/Leads/{lead_id}", params={"fields": "Description"}, headers=await _h())
            desc = (r.json().get("data", [{}])[0].get("Description") or "") if r.status_code == 200 else ""
            champs["Description"] = (desc + "\n" if desc else "") + f"[{datetime.now():%d/%m/%Y}] Agent : {note}"
            modifies.append("Description")
        if not modifies:
            return json.dumps({"erreur": "Aucun champ à modifier."}, ensure_ascii=False)
        r = await client.put(f"{ZOHO_CRM}/Leads", json={"data": [champs]}, headers=await _h())
        r.raise_for_status()
        data = r.json()["data"][0]
        if data.get("status") != "success":
            return json.dumps({"erreur": data}, ensure_ascii=False)
        return json.dumps({"lead_id": lead_id, "modifie": modifies}, ensure_ascii=False)


@agents.tool
async def creer_prospect(
    nom: str,
    prenom: str = "",
    civilite: str = "",
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
    champs de dédoublonnage (Nom_normalis, T_l_phone_E164, SIREN), la Civilité (Salutation :
    M., Mme, Mlle... résolue automatiquement en valeur API valide du CRM), puis
    conversion avec affaire au stade Qualification dans le Pipeline Investisseurs.
    À appeler uniquement si verifier_doublons renvoie aucun_match. Renvoie les ids créés en JSON."""
    tel = _norm_phone(telephone)
    async with httpx.AsyncClient(timeout=60) as client:
        salut, _ = await _resolve(client, civilite, "Leads", "Salutation", LEADS_LAYOUT_ID)
        salut = salut or _salutation(civilite)
        type_api, _ = await _resolve(client, type_investisseur, "Leads", "Type_d_investisseur", LEADS_LAYOUT_ID)
        source_api, _ = await _resolve(client, source_detaillee, "Leads", "Source_d_taill_e", LEADS_LAYOUT_ID)
    payload = {
        "data": [
            {
                "Layout": {"id": LEADS_LAYOUT_ID},
                "Last_Name": nom,
                "First_Name": prenom or "",
                "Salutation": salut or None,
                "Company": societe or (f"{prenom} {nom}"),
                "Email": email or None,
                "Phone": telephone or None,
                "City": ville or None,
                "Nom_normalis": _norm((prenom or "") + nom),
                "T_l_phone_E164": tel or None,
                "SIREN": siren_lie or None,
                "Type_d_investisseur": type_api or type_investisseur,
                "Source_d_taill_e": source_api or source_detaillee,
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
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{ZOHO_CRM}/Calls",
            params={"per_page": 100, "sort_by": "Created_Time", "sort_order": "desc"},
            headers=await _h(),
        )
        r.raise_for_status()
        appels = r.json().get("data", [])

    tel2lead = {}
    async with httpx.AsyncClient(timeout=60) as client:
        rl = await client.get(f"{ZOHO_CRM}/Leads", params={"per_page": 200, "fields": "id,Full_Name,T_l_phone_E164,Phone,Company"}, headers=await _h())
        rl.raise_for_status()
        for l in rl.json().get("data", []):
            for t in (l.get("T_l_phone_E164"), l.get("Phone")):
                if t:
                    tel2lead[_norm_phone(t)] = l

    resultats = []
    for a in appels[:100]:
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


@agents.tool(annotations=agents.ToolAnnotations(read_only_hint=True))
async def lister_tags(module: str = "Leads") -> str:
    """Liste les tags (étiquettes de campagne) d'un module Zoho (Leads par défaut) :
    nom de chaque tag et nombre de fiches associées. Lecture seule."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{ZOHO_CRM}/settings/tags", params={"module": module}, headers=await _h())
        r.raise_for_status()
        tags = r.json().get("tags", [])
        return json.dumps({"module": module, "tags": [{"name": t.get("name"), "nb_fiches": t.get("count")} for t in tags]}, ensure_ascii=False)
