"""Tools Zoho CRM de l'agent investisseurs (auth par secrets d'environnement)."""

import json
import os
import re
import unicodedata
from datetime import datetime

import httpx

from mistralai import agents

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"
PAPPERS_API = "https://api.pappers.fr/v2"
LEADS_LAYOUT_ID = "968010000002223131"  # disposition « Investisseurs » (Leads)
DEALS_LAYOUT_ID = "968010000002223015"  # disposition « Investisseurs » (Deals)
PIPELINE = "Pipeline Investisseurs"

# Champs téléphoniques du layout : Phone = « Téléphone » (fixe), Mobile = « Portable »,
# T_l_phone_E164 = numéro de référence E164 (le portable en priorité).

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
    portable: str = "",
    siren: str = "",
    ville: str = "",
) -> str:
    """Recherche les doublons AVANT toute création de prospect investisseur (3 passes :
    SIREN société liée, puis email/téléphone E164 (portable en priorité, sinon fixe),
    puis nom+prénom normalisés). Renvoie un verdict JSON : aucun_match / match_certain
    (avec id de fiche) / rapprochements_possibles (avec scores). Toujours appeler avant
    creer_prospect."""
    email = (email or "").strip().lower()
    tel = _norm_phone(portable or telephone)
    nom_norm = _norm((prenom or "") + (nom or ""))

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
    resultats = []
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


@agents.tool(annotations=agents.ToolAnnotations(read_only_hint=True))
async def chercher_siren(nom_societe: str, ville: str = "", code_postal: str = "") -> str:
    """Recherche une société dans la base Pappers (registre national des entreprises)
    et renvoie jusqu'à 5 candidats : raison sociale, SIREN, ville, code postal, rue du
    siège, état (Actif, etc.) et téléphone si présent dans les résultats (rare : le registre
    national ne publie pas les téléphones des sociétés, ne PAS compter dessus).
    À utiliser pour
    renseigner le champ SIREN d'un prospect dont la société est connue mais le SIREN
    absent du listing, via maj_prospect (paramètre siren) ou creer_prospect
    (paramètre siren_lie) ; sert AUSSI à trouver la ligne standard de la société quand
    le listing n'a aucun téléphone. Si plusieurs candidats se valent, présente-les à
    Olivier et attends son choix. Si Pappers échoue ou ne renvoie rien, repli
    automatique sur l'API publique recherche-entreprises.api.gouv.fr (SIREN/adresse,
    sans téléphone). Lecture seule, hors Zoho."""
    params = {"q": nom_societe, "per_page": 5}
    if ville:
        params["ville"] = ville
    if code_postal:
        params["code_postal"] = code_postal
    candidats = []
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(
                f"{PAPPERS_API}/recherche",
                params={**params, "api_token": os.environ.get("PAPPERS_API_KEY", "")},
            )
            r.raise_for_status()
            resultats = r.json().get("resultats", [])
        except Exception:
            resultats = []
        for e in resultats[:5]:
            siege = e.get("siege") or {}
            etat = e.get("etat")
            if not isinstance(etat, str):
                etat = (etat or {}).get("etat")
            tel = siege.get("telephone") or e.get("telephone")
            candidats.append(
                {
                    "raison_sociale": e.get("nom_entreprise"),
                    "siren": e.get("siren"),
                    "ville_siege": siege.get("ville"),
                    "cp_siege": siege.get("code_postal"),
                    "rue_siege": siege.get("adresse") or (siege.get("adresse_ligne_1") if isinstance(siege.get("adresse_ligne_1"), str) else None),
                    "etat": etat,
                    "telephone_standard": tel,
                    "source": "pappers",
                }
            )
    if not candidats:
        # Repli public sans clé : recherche-entreprises.api.gouv.fr (pas de téléphone)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                rp = {"q": nom_societe, "per_page": 5}
                if code_postal:
                    rp["code_postal"] = code_postal
                rg = await client.get("https://recherche-entreprises.api.gouv.fr/search", params=rp)
                rg.raise_for_status()
                for res in (rg.json().get("resultats") or [])[:5]:
                    siege = res.get("siege") or {}
                    adr = siege.get("adresse") or {}
                    candidats.append(
                        {
                            "raison_sociale": res.get("nom_complet"),
                            "siren": res.get("siren"),
                            "ville_siege": (siege.get("libelle_commune") or {}).get("value") if isinstance(siege.get("libelle_commune"), dict) else siege.get("libelle_commune"),
                            "cp_siege": (siege.get("code_postal") or {}).get("value") if isinstance(siege.get("code_postal"), dict) else siege.get("code_postal"),
                            "rue_siege": adr.get("l1") or adr.get("l2") if isinstance(adr, dict) else None,
                            "etat": "Actif" if res.get("etat_administratif") == "A" else res.get("etat_administratif"),
                            "telephone_standard": None,
                            "source": "api.gouv (repli, sans téléphone)",
                        }
                    )
        except Exception:
            candidats = []
    return json.dumps({"recherche": nom_societe, "candidats": candidats}, ensure_ascii=False)


@agents.tool
async def maj_prospect(
    lead_id: str,
    civilite: str = "",
    statut_prospect: str = "",
    note: str = "",
    email: str = "",
    telephone: str = "",
    portable: str = "",
    ville: str = "",
    rue: str = "",
    code_postal: str = "",
    region: str = "",
    siren: str = "",
    type_investisseur: str = "",
    source_detaillee: str = "",
) -> str:
    """Met à jour un lead Zoho EXISTANT (ne crée jamais de fiche). Identifie la fiche par
    son lead_id. Champs modifiables (seuls ceux fournis sont changés) : Civilité
    (civilite : M./Mme/Mlle...), Status du Prospect (statut_prospect : donner le libellé
    affiché dans Zoho, ex. "A contacter dans le futur" — la valeur API valide est résolue
    automatiquement depuis les métadonnées du CRM), email, telephone (ligne fixe → champ
    Téléphone), portable (smartphone → champ Portable, distinct du Téléphone), ville, rue
    (Street), code_postal (Zip_Code), region (State),
    SIREN de la société liée (siren, à obtenir via chercher_siren si absent du listing),
    type d'investisseur, source détaillée. Le champ Téléphone E164 est recalculé depuis
    le portable si fourni, sinon le téléphone. note : texte ajouté en fin de Description
    au format [JJ/MM/AAAA] Agent : note. Renvoie JSON {lead_id, modifie: [champs]}."""
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
            modifies.append("Téléphone")
        if portable:
            champs["Mobile"] = portable
            modifies.append("Portable")
        if telephone or portable:
            champs["T_l_phone_E164"] = _norm_phone(portable or telephone)
            modifies.append("Téléphone E164")
        if ville:
            champs["City"] = ville
            modifies.append("Ville")
        if rue:
            champs["Street"] = rue
            modifies.append("Rue")
        if code_postal:
            champs["Zip_Code"] = code_postal
            modifies.append("Code postal")
        if region:
            champs["State"] = region
            modifies.append("Région")
        if siren:
            siren = re.sub(r"\D", "", siren)
            if len(siren) == 9:
                champs["SIREN"] = siren
                modifies.append("SIREN")
            else:
                return json.dumps({"erreur": f"SIREN invalide (9 chiffres attendus) : {siren}"}, ensure_ascii=False)
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
    portable: str = "",
    ville: str = "",
    rue: str = "",
    code_postal: str = "",
    region: str = "",
    siren_lie: str = "",
    type_investisseur: str = "Personne Physique",
    source_detaillee: str = "Listing",
    tag_campagne: str = "",
) -> str:
    """Crée le prospect investisseur dans Zoho : lead (disposition Investisseurs) avec les
    champs de dédoublonnage (Nom_normalis, T_l_phone_E164, SIREN), la Civilité (Salutation :
    M., Mme, Mlle... résolue automatiquement en valeur API valide du CRM). AUCUNE conversion :
    la fiche reste dans le module Prospects, la conversion en Contact/Compte est
    une décision manuelle d'Olivier dans Zoho. Distinction
    téléphonique : telephone = ligne fixe (champ Téléphone), portable = smartphone (champ
    Portable) ; le champ Téléphone E164 vaut le portable si fourni, sinon le fixe.
    À appeler uniquement si verifier_doublons renvoie aucun_match. Si la société est
    connue mais le SIREN absent du listing, obtenir le SIREN via chercher_siren.
    Adresse : ville (City), rue (Street), code_postal (Zip_Code),
    region (State, ex. Nouvelle-Aquitaine). Renvoie les ids créés en JSON."""
    tel = _norm_phone(portable or telephone)
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
                "Mobile": portable or None,
                "City": ville or None,
                "Street": rue or None,
                "Zip_Code": code_postal or None,
                "State": region or None,
                "Nom_normalis": _norm((prenom or "") + nom),
                "T_l_phone_E164": tel or None,
                "SIREN": (re.sub(r"\D", "", siren_lie) or None) if siren_lie else None,
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

        return json.dumps(
            {"lead_id": lead_id, "message": "Prospect créé dans le module Prospects (aucune conversion)"},
            ensure_ascii=False,
        )


@agents.tool
async def etiqueter(lead_ids: str, tag: str) -> str:
    """Pose une balise (tag) Zoho sur un ou plusieurs leads existants : lead_ids = ids
    séparés par des virgules, tag = nom de la balise (ex. "distributeurs", "en vrac").
    La balise est créée automatiquement dans le module Leads si elle n'existe pas encore.
    Renvoie JSON {tag, poses: [ids], erreurs}. Pour lister les fiches portant une ou
    plusieurs balises, utiliser rechercher_par_tags."""
    ids = [i.strip() for i in (lead_ids or "").split(",") if i.strip()]
    tag = (tag or "").strip()
    if not ids or not tag:
        return json.dumps({"erreur": "lead_ids (ids séparés par des virgules) et tag requis"}, ensure_ascii=False)
    poses: list = []
    erreurs: list = []
    async with httpx.AsyncClient(timeout=60) as client:
        # s'assure que la balise existe (ignoré si déjà présente)
        await client.post(
            f"{ZOHO_CRM}/settings/tags",
            params={"module": "Leads"},
            json={"tags": [{"name": tag[:25]}]},
            headers=await _h(),
        )
        for lid in ids:
            r = await client.post(
                f"{ZOHO_CRM}/Leads/{lid}/actions/add_tags",
                json={"over_write": "false", "tags": [{"name": tag[:25]}]},
                headers=await _h(),
            )
            if r.status_code in (200, 201):
                poses.append(lid)
            else:
                erreurs.append({"lead_id": lid, "http": r.status_code, "corps": r.text[:150]})
    return json.dumps({"tag": tag[:25], "nb_poses": len(poses), "poses": poses, "erreurs": erreurs}, ensure_ascii=False)


@agents.tool(annotations=agents.ToolAnnotations(read_only_hint=True))
async def rechercher_par_tags(tags: str, module: str = "Leads") -> str:
    """Sélection multicritères par balises : renvoie les fiches du module (Leads par
    défaut) qui portent TOUTES les balises indiquées, tags séparés par des virgules
    (ex. "distributeurs,en vrac"). Une seule balise suffit aussi (liste des fiches de
    cette balise). Renvoie pour chaque fiche : id, nom, société, ville, statut du
    prospect, portable et téléphone. Lecture seule."""
    liste = [t.strip() for t in (tags or "").split(",") if t.strip()]
    if not liste:
        return json.dumps({"erreur": "au moins une balise requise (séparées par des virgules)"}, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=60) as client:
        ensembles = []
        for t in liste:
            r = await client.get(
                f"{ZOHO_CRM}/{module}/search",
                params={"criteria": f"(Tag:equals:{t[:25]})", "per_page": 200},
                headers=await _h(),
            )
            if r.status_code == 204:
                ensembles.append({})
                continue
            r.raise_for_status()
            ensembles.append({x.get("id"): x for x in r.json().get("data", [])})
        communs = set.intersection(*(set(e) for e in ensembles)) if len(ensembles) > 1 else set(ensembles[0])
        fiches = [ensembles[0][i] for i in communs]
    resultats = [
        {
            "id": f.get("id"),
            "nom": f.get("Full_Name"),
            "societe": f.get("Company"),
            "ville": f.get("City"),
            "statut": f.get("Lead_Status"),
            "portable": f.get("Mobile"),
            "telephone": f.get("Phone"),
        }
        for f in fiches
    ]
    return json.dumps({"tags": liste, "nb_fiches": len(resultats), "fiches": resultats}, ensure_ascii=False)


@agents.tool
async def planifier_relance(
    sujet: str,
    lead_id: str,
    date_echeance: str,
    heure_rappel: str = "08:30:00",
    priorite: str = "Normal",
) -> str:
    """Crée une tâche de relance liée au prospect (lead Zoho). date_echeance au format
    AAAA-MM-JJ (champ Due_Date de la tâche) ; heure_rappel (hh:mm:ss, heure de Paris) est
    consignée dans la tâche car Zoho n'expose pas de champ rappel sur les tâches via l'API.
    Cadences usuelles : J+7 sans réponse, J+7 après RDV, J+30 après signature.
    Refuse un lead introuvable dans le module Prospects (fiche convertie en contact)."""
    async with httpx.AsyncClient(timeout=30) as client:
        # Un lead converti n'est plus adressable en écriture : la tâche échouerait.
        rl = await client.get(f"{ZOHO_CRM}/Leads/{lead_id}", params={"fields": "id,Full_Name"}, headers=await _h())
        if rl.status_code != 200:
            return json.dumps({"erreur": f"lead_id introuvable dans le module Prospects : {lead_id} (fiche convertie en contact ?)", "http": rl.status_code}, ensure_ascii=False)

        base = {
            "Subject": sujet,
            "Description": f"Relance prévue le {date_echeance}, rappel souhaité à {heure_rappel} (heure de Paris).",
            "Status": "Not Started",
            "$se_module": "Leads",
            "What_Id": lead_id,
        }
        r = await client.post(f"{ZOHO_CRM}/Tasks", json={"data": [dict(base, Due_Date=date_echeance, Priority=priorite)]}, headers=await _h())
        if r.status_code >= 400 and priorite:
            # Priority est une picklist : si la valeur est refusée, on la retire.
            r = await client.post(f"{ZOHO_CRM}/Tasks", json={"data": [dict(base, Due_Date=date_echeance)]}, headers=await _h())
        if r.status_code >= 400:
            return json.dumps({"erreur": r.text[:300], "http": r.status_code}, ensure_ascii=False)
        data = r.json()["data"][0]
        return json.dumps({"tache": data.get("details", data), "rappel_souhaite": f"{date_echeance} {heure_rappel}"}, ensure_ascii=False)


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
        rl = await client.get(f"{ZOHO_CRM}/Leads", params={"per_page": 200, "fields": "id,Full_Name,T_l_phone_E164,Phone,Mobile,Company"}, headers=await _h())
        rl.raise_for_status()
        for l in rl.json().get("data", []):
            for t in (l.get("T_l_phone_E164"), l.get("Mobile"), l.get("Phone")):
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
    """Liste les balises (tags) d'un module Zoho (Leads par défaut) : nom de chaque
    balise et nombre de fiches associées. Lecture seule."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{ZOHO_CRM}/settings/tags", params={"module": module}, headers=await _h())
        r.raise_for_status()
        tags = r.json().get("tags", [])
        return json.dumps({"module": module, "tags": [{"name": t.get("name"), "nb_fiches": t.get("count")} for t in tags]}, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Étude de capacité d'investissement (SylvaGreen Conseils)
# ---------------------------------------------------------------------------
ETUDES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etudes")
_VARIANTES_ETUDE = {
    "societe": ("template_societe.html", 7, 4),
    "particulier_transmission": ("template_particulier_transmission.html", 9, 6),
    "particulier": ("template_particulier.html", 7, 4),
}


@agents.tool
async def generer_etude(lead_id: str, variante: str, destinataire: str, couverture_html: str, pages: str) -> str:
    """Génère l'étude de capacité d'investissement SylvaGreen Conseils d'un prospect :
    remplit le gabarit HTML validé (tokens), convertit en PDF A4 sans marges (WeasyPrint),
    ATTACHE le PDF à la fiche Prospect du lead (remplace une pièce jointe homonyme),
    ajoute une note dans la Description, puis DÉTRUIT le HTML : seul le PDF existe.
    À appeler automatiquement juste après creer_prospect, et à chaque demande d'Olivier
    de mise à jour de l'étude (nouvelles informations apprises au téléphone par ex.).
    - variante : "societe" (7 chapitres, 4 pages), "particulier_transmission"
      (9 chapitres, 6 pages) ou "particulier" (7 chapitres, 4 pages).
    - destinataire : nom de la société OU civilité + prénom + nom du particulier
      (ex. "M. Jean-Louis DUCLAUX") ; sert aux en-têtes et au nom du fichier :
      "Étude de capacité d'investissement — {destinataire}.pdf".
    - couverture_html : bloc identité de la colonne droite de la couverture
      (société : forme juridique, représentant, adresse, SIREN ; particulier :
      nom, adresse, SANS SIREN ni forme juridique).
    - pages : JSON {"1": "<HTML de la page 1>", ...} — contenu des pages de chapitres
      rédigé selon les règles du prompt.
    Renvoie JSON {nom_pdf, variante, nb_chapitres, nb_pages, attachment_id, message}."""
    if variante not in _VARIANTES_ETUDE:
        return json.dumps({"erreur": f"variante inconnue : {variante}. Valeurs : {list(_VARIANTES_ETUDE)}"}, ensure_ascii=False)
    gabarit, nb_chapitres, nb_pages = _VARIANTES_ETUDE[variante]
    chemin = os.path.join(ETUDES_DIR, gabarit)
    if not os.path.exists(chemin):
        return json.dumps({"erreur": f"gabarit introuvable : {chemin}"}, ensure_ascii=False)
    try:
        corps = json.loads(pages)
    except Exception as e:
        return json.dumps({"erreur": f"pages n'est pas un JSON valide : {e}"}, ensure_ascii=False)

    html = open(chemin, encoding="utf-8").read()
    logo_path = os.path.join(ETUDES_DIR, "logo_base64.txt")
    if not os.path.exists(logo_path):
        return json.dumps({"erreur": f"logo introuvable : {logo_path}"}, ensure_ascii=False)
    logo = "".join(open(logo_path, encoding="utf-8").read().split())
    if html.count("{{LOGO}}") < nb_pages + 1:
        return json.dumps({"erreur": "logo manquant sur une ou plusieurs feuilles"}, ensure_ascii=False)
    html = html.replace("{{LOGO}}", "data:image/png;base64," + logo)
    html = html.replace("{{DATE}}", f"{datetime.now():%d/%m/%Y}")
    html = html.replace("{{DESTINATAIRE}}", destinataire)
    html = html.replace("{{COUV_DESTINATAIRE}}", couverture_html)
    for n in range(1, nb_pages + 1):
        html = html.replace("{{PAGE%d_BODY}}" % n, corps.get(str(n), ""))
    if "{{" in html:
        i = html.index("{{")
        return json.dumps({"erreur": f"token non substitué près de : {html[i:i+40]!r}"}, ensure_ascii=False)
    if "None" in html:
        return json.dumps({"erreur": "valeur None détectée dans le contenu — renseigner ou reformuler"}, ensure_ascii=False)
    if html.count("<h2>") < nb_chapitres:
        return json.dumps({"erreur": f"{html.count('<h2>')} chapitres <h2> trouvés, {nb_chapitres} attendus"}, ensure_ascii=False)

    # HTML -> PDF A4 (marges déjà à 0 dans le gabarit), puis HTML détruit
    import tempfile
    from weasyprint import HTML as WHTML
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    WHTML(string=html).write_pdf(tmp.name)
    pdf = open(tmp.name, "rb").read()
    os.unlink(tmp.name)  # PDF temporaire local ; la version durable est la pièce jointe Zoho
    del html  # le HTML ne doit exister nulle part : il vivait uniquement en mémoire

    nom_pdf = f"Étude de capacité d'investissement — {destinataire}.pdf"
    async with httpx.AsyncClient(timeout=120) as client:
        # remplace une pièce jointe homonyme (rejouable)
        ra = await client.get(f"{ZOHO_CRM}/Leads/{lead_id}/Attachments", headers=await _h())
        if ra.status_code == 200:
            for att in ra.json().get("data", []):
                if att.get("File_Name") == nom_pdf:
                    await client.delete(f"{ZOHO_CRM}/Leads/{lead_id}/Attachments/{att['id']}", headers=await _h())
        r = await client.post(
            f"{ZOHO_CRM}/Leads/{lead_id}/Attachments",
            files={"file": (nom_pdf, pdf, "application/pdf")},
            headers=await _h(),
        )
        if r.status_code >= 400:
            return json.dumps({"erreur": f"téléversement échoué : {r.status_code} {r.text[:200]}"}, ensure_ascii=False)
        data = r.json()["data"][0]
        if data.get("status") != "success":
            return json.dumps({"erreur": data}, ensure_ascii=False)
        # note dans la Description
        rd = await client.get(f"{ZOHO_CRM}/Leads/{lead_id}", params={"fields": "Description"}, headers=await _h())
        desc = (rd.json().get("data", [{}])[0].get("Description") or "") if rd.status_code == 200 else ""
        note = f"[{datetime.now():%d/%m/%Y}] Agent : Étude de capacité d'investissement SylvaGreen Conseils ({nom_pdf} joint)"
        await client.put(f"{ZOHO_CRM}/Leads", json={"data": [{"id": lead_id, "Description": (desc + "\n" if desc else "") + note}]}, headers=await _h())
        return json.dumps(
            {"nom_pdf": nom_pdf, "variante": variante, "nb_chapitres": nb_chapitres, "nb_pages": nb_pages, "attachment_id": data.get("details", {}).get("id"), "message": "Étude générée et jointe à la fiche (HTML détruit)"},
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# 13e outil : chercher un prospect par prénom + nom ou société (identification
# d'une fiche EXISTANTE à partir d'une demande d'Olivier ; renvoie le lead_id).
# ---------------------------------------------------------------------------
_MOTS_VIDES = {
    "sarl", "sas", "sasu", "eurl", "sa", "sci", "snc", "sca", "gmbh",
    "les", "la", "le", "de", "du", "des", "et", "pour", "sur", "ste", "st",
}
_CIVILITES_API = {"Mr.": "M.", "Mrs.": "Mme", "Ms.": "Mlle", "Dr.": "Dr", "Prof.": "Pr"}


def _mots_requete(requete: str) -> list:
    mots = re.split(r"[^0-9A-Za-zÀ-ÖØ-öø-ÿ']+", requete or "")
    mots = [m.strip("'") for m in mots if len(m.strip("'")) >= 3]
    mots = [m for m in mots if m.lower() not in _MOTS_VIDES]
    return list(dict.fromkeys(mots))[:4]


@agents.tool(annotations=agents.ToolAnnotations(read_only_hint=True))
async def chercher_prospect(requete: str) -> str:
    """Retrouve une fiche Prospect (Lead) EXISTANTE à partir d'une demande d'Olivier,
    qui désigne ses prospects par prénom + nom (« Julien de Sloovere ») ou par société
    (« De Sloovere », « ALAIN THEVENIN SARL »). C'est LA façon d'identifier un prospect
    avant tout maj_prospect / planifier_relance / generer_etude : NE PAS utiliser
    lire_listing ni d'autres détours pour identifier une personne.
    Recherche souple : chaque mot significatif de la requête (hors SARL/SAS/de/la/...)
    est cherché dans Nom, Prénom, Société et Email (commence par, insensible à la
    casse), les résultats sont fusionnés puis classés par nombre de mots reconnus.
    Renvoie jusqu'à 5 fiches : JSON {nb, resultats: [{lead_id, civilite, prenom, nom,
    nom_complet, societe, telephone, portable, email, ville, siren, score}]}. En cas
    d'homonymes, demande à Olivier de préciser (société, ville) ; à 0 résultat, avec
    le nom de famille seul ou la société seule."""
    mots = _mots_requete(requete)
    if not mots:
        return json.dumps(
            {"erreur": "requete trop courte : donner prénom + nom ou la société"},
            ensure_ascii=False,
        )
    vus: dict = {}
    for mot in mots:
        crit = (
            f"((Last_Name:starts_with:{mot})"
            f"or(First_Name:starts_with:{mot})"
            f"or(Company:starts_with:{mot})"
            f"or(Email:starts_with:{mot}))"
        )
        try:
            for r in await _search("Leads", crit):
                vus.setdefault(str(r.get("id")), r)
        except Exception:
            continue  # mot non trouvé (ou critère refusé) : on passe au suivant

    if not vus:
        return json.dumps(
            {"nb": 0, "resultats": [],
             "message": "aucun prospect trouvé : reformuler avec le nom de famille seul "
                        "ou la société seule ; sinon la fiche n'existe pas encore "
                        "(la créer via creer_prospect)"},
            ensure_ascii=False,
        )

    def _score(r: dict) -> int:
        champs = " ".join(
            _norm(x) for x in (r.get("Last_Name", ""), r.get("First_Name", ""), r.get("Company", ""), r.get("Email", ""))
        ).split()
        s = 0
        for mot in mots:
            nm = _norm(mot)
            if any(w.startswith(nm) for w in champs):
                s += 1
        return s

    classement = sorted(vus.values(), key=_score, reverse=True)[:5]
    resultats = [
        {
            "lead_id": r.get("id"),
            "civilite": _CIVILITES_API.get(r.get("Salutation", ""), r.get("Salutation", "")),
            "prenom": r.get("First_Name", ""),
            "nom": r.get("Last_Name", ""),
            "nom_complet": " ".join(x for x in (r.get("First_Name", ""), r.get("Last_Name", "")) if x),
            "societe": r.get("Company", ""),
            "telephone": r.get("Phone", ""),
            "portable": r.get("Mobile", ""),
            "email": r.get("Email", ""),
            "ville": r.get("City", ""),
            "siren": r.get("SIREN", ""),
            "score": _score(r),
        }
        for r in classement
    ]
    return json.dumps({"nb": len(resultats), "resultats": resultats}, ensure_ascii=False)
