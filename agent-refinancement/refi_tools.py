"""Outils Zoho CRM de l'agent refinancement (crédit-bail) — SylvaGreen Capital.

Deux outils (progression actée : étapes 1-2 du process uniquement) :
- enregistrer_demande : crée le DOSSIER (Affaire du Pipeline « Refinancement »,
  disposition Refinancement, étape Qualification) dès la prise de demande.
- preparer_client    : crée/complète la fiche Compte du bénéficiaire depuis le
  registre national (Pappers) et renvoie le dossier de bancabilité.

Le module Devis (Quotes) porte les VERSIONS de devis ; il exige des lignes produit
(sous-formulaire Quoted_Items obligatoire) : il est créé au chiffrage, quand
l'équipement réel est connu — jamais à la prise de demande.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime

import httpx

from mistralai import agents

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"
PAPPERS_API = "https://api.pappers.fr/v2"
PIPELINE_REFI = "Refinancement"
DEALS_REFI_LAYOUT_ID = "968010000002421014"  # disposition « Refinancement » des Deals
STAGES_FINALES = {"Closed Won", "Closed Lost", "Closed Lost to Competition", "Refus organisme"}

_access_token: str | None = None
_token_ts: float = 0.0
_picklist_cache: dict = {}


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _mots(texte: str, max_mots: int = 4) -> list:
    """Mots significatifs (alphanumériques >= 3 lettres), formes juridiques exclues."""
    cretes = {
        "sarl", "sas", "sasu", "sa", "sci", "snc", "eurl", "ets", "ste", "see",
        "groupe", "societe", "scierie", "scieries", "les", "la", "le", "de", "des",
        "du", "et", "the", "inc", "gmbh", "esp", "demande", "devis",
    }
    mots = re.split(r"[^0-9A-Za-zÀ-ÖØ-öø-ÿ]+", texte or "")
    garde = [m for m in mots if len(m) >= 3 and _norm(m) not in cretes]
    return list(dict.fromkeys(garde))[:max_mots]


def _siren_de(texte: str) -> str:
    m = re.search(r"\b(\d{9})\b", texte or "")
    return m.group(1) if m else ""


def _montant_float(montant) -> float | None:
    """Saisie libre (« 150 000 € », 150000, « 150,5 k ») -> float, sinon None."""
    if montant is None or montant == "":
        return None
    s = str(montant).strip().lower().replace("€", "").replace(" ", "").replace("\u00a0", "")
    k = 1000.0 if s.endswith("k") else 1.0
    if s.endswith("k"):
        s = s[:-1]
    s = re.sub(r"(?<=\d)\.(?=\d{3}(\D|$))", "", s)
    s = s.replace(",", ".")
    try:
        return float(s) * k
    except ValueError:
        return None


async def _token() -> str:
    """Access token Zoho, cache ~50 min max (les tokens expirent à 1 h).
    Règle apprise : JAMAIS de cache de token sans TTL, sinon cascade de 401."""
    global _access_token, _token_ts
    if _access_token and (time.time() - _token_ts) < 3000:
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
            raise RuntimeError(f"Token Zoho refusé : {body}")
        _access_token = body["access_token"]
        _token_ts = time.time()
        return _access_token


async def _h() -> dict:
    return {"Authorization": "Zoho-oauthtoken " + await _token()}


async def _search(module: str, criteria: str, fields: str = "") -> list:
    params = {"criteria": criteria, "per_page": 25}
    if fields:
        params["fields"] = fields
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{ZOHO_CRM}/{module}/search", params=params, headers=await _h())
        if r.status_code == 204:
            return []
        if r.status_code == 400:
            # critère refusé (opérateur inconnu etc.) : ne casse pas l'appelant
            return []
        r.raise_for_status()
        return r.json().get("data", [])


async def _picklist(client, module: str, champ_api: str, layout_id: str = "") -> list:
    """[(valeur_api, libellé_affiché)] lus dans les métadonnées du CRM.
    Aucune valeur codée en dur : source de vérité = le CRM."""
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
                valeurs = [(v.get("actual_value"), v.get("display_value")) for v in (f.get("pick_list_values") or [])]
                break
    if layout_id and not valeurs:
        return await _picklist(client, module, champ_api, "")
    if valeurs:
        _picklist_cache[cle] = valeurs
    return valeurs


async def _resolve(client, saisie: str, module: str, champ_api: str, layout_id: str = ""):
    """Libellé FR ou valeur API -> valeur API valide du picklist (normalisation
    accents/casse). Renvoie (valeur_api ou None, valeurs_valides)."""
    valeurs = await _picklist(client, module, champ_api, layout_id)
    n = _norm(saisie)
    if n:
        for actual, libelle in valeurs:
            if n in (_norm(actual), _norm(libelle)):
                return actual, valeurs
    return None, valeurs


async def _trouver_compte(client_http, cible: str, siren: str = "") -> dict:
    """Localise le compte bénéficiaire (par SIREN via le champ SIRET, sinon par
    mots du nom). Renvoie {verdict: introuvable|unique|plusieurs, comptes: [...] }."""
    siren = siren or _siren_de(cible)
    if siren:
        f = await _search("Accounts", f"(SIRET:equals:{siren})", "id,Account_Name,SIRET,Industry")
        if f:
            return {"verdict": "unique", "comptes": f}
    vus: dict = {}
    mots = _mots(cible, 6)
    if not mots:
        return {"verdict": "introuvable", "comptes": [], "siren": siren}
    for mot in mots:
        for f in await _search("Accounts", f"(Account_Name:starts_with:{mot})", "id,Account_Name,SIRET,Industry"):
            nm = _norm(f.get("Account_Name") or "")
            if _norm(mot) in nm:  # filtre côté client : évite les faux positifs sur préfixe
                vus.setdefault(str(f.get("id")), f)
    comptes = [c for c in vus.values()
               if all(_norm(m) in _norm(c.get("Account_Name") or "") for m in mots)]
    if not comptes:
        return {"verdict": "introuvable", "comptes": [], "siren": siren}
    if len(comptes) == 1:
        return {"verdict": "unique", "comptes": comptes, "siren": siren}
    return {"verdict": "plusieurs", "comptes": comptes[:5], "siren": siren}


async def _lire_description(client_http, module: str, record_id: str) -> str:
    r = await client_http.get(f"{ZOHO_CRM}/{module}/{record_id}", params={"fields": "Description"}, headers=await _h())
    if r.status_code == 200:
        return (r.json().get("data", [{}])[0].get("Description") or "")
    return ""


def _note(description: str, texte: str) -> str:
    return (description + "\n" if description else "") + f"[{datetime.now():%d/%m/%Y}] Agent refinancement : {texte}"


# ---------------------------------------------------------------------------
# Étape 1 — enregistrer la demande (dossier dans le Pipeline Refinancement)
# ---------------------------------------------------------------------------
@agents.tool
async def enregistrer_demande(
    client: str,
    equipement: str,
    source: str = "Distributeur partenaire",
    distributeur: str = "",
    type_produit: str = "",
    prix_ht: str = "",
    prix_ttc: str = "",
    note: str = "",
) -> str:
    """ENREGISTRE une demande de refinancement d'équipement (étape 1 du process) :
    crée le DOSSIER dans Zoho CRM = une Affaire du Pipeline « Refinancement »
    (disposition Refinancement, étape « Qualification »), rattachée au compte du
    client bénéficiaire. RÈGLE D'OR : la demande précède TOUT chiffrage ; dès qu'une
    demande existe (même approximative), c'est CET outil qui l'enregistre.
    - client : nom de la société bénéficiaire OU son SIREN (9 chiffres). Le compte
      doit déjà exister dans le CRM ; sinon appeler preparer_client d'abord (ou
      demander le SIREN à Olivier).
    - equipement : description complète au moment de la prise de demande : nature,
      marque/modèle, prix connu, livraison, mise en service, contraintes (caution,
      assurance, CEE/subventions mobilisables). Les détails manquants seront
      complétés à l'étape qualification.
    - source : canal d'entrée : « Distributeur partenaire » (défaut), « Client final »,
      « Site web », « Recommandation ».
    - distributeur : nom OU SIREN du distributeur d'équipement apporteur (Carbon Stop,
      VBI/Kalfass...) : s'il existe dans le CRM il est rattaché comme Fournisseur de
      l'affaire ; sinon son nom reste en trace dans la description (pas de fiche
      créée ici).
    - type_produit : « Cession/Location » ou « Location/Cession » si déjà connu
      (sinon vide : c'est l'étape 4 qui tranche).
    - prix_ht / prix_ttc : prix d'acquisition estimé de l'équipement (l'un, l'autre
      ou aucun).
    - note : complément libre (ex. éléments appris au téléphone).
    Le module Devis (versions de devis) sera créé au chiffrage, une fois l'équipement
    réel connu (les devis Zoho exigent des lignes produit). L'outil ne touche JAMAIS
    le champ Amount de l'affaire (= future commission, pas le montant financé).
    Idempotent : si une affaire Refinancement OUVERTE existe déjà pour ce client et
    cet équipement, elle est renvoyée sans rien créer.
    Renvoie JSON {statut: cree|dossier_existant, deal_id, dossier, compte, etape,
    champs, warnings?}."""
    async with httpx.AsyncClient(timeout=60) as chttp:
        # --- 1. compte bénéficiaire ---
        trouve = await _trouver_compte(chttp, client)
        if trouve["verdict"] == "introuvable":
            return json.dumps({
                "erreur": f"Compte introuvable pour « {client} ». Appeler preparer_client d'abord "
                          f"(il crée la fiche depuis le registre national si le SIREN est trouvé), "
                          f"ou demander le SIREN à Olivier.",
            }, ensure_ascii=False)
        if trouve["verdict"] == "plusieurs":
            return json.dumps({
                "erreur": "Plusieurs comptes correspondent : préciser (SIREN de préférence).",
                "candidats": [{"id": f.get("id"), "nom": f.get("Account_Name"), "siren": f.get("SIRET")} for f in trouve["comptes"]],
            }, ensure_ascii=False)
        compte = trouve["comptes"][0]
        compte_id, compte_nom = compte.get("id"), compte.get("Account_Name")

        # --- 2. doublon de dossier ? affaires ouvertes du client ---
        ouverts: list = []
        r = await chttp.get(
            f"{ZOHO_CRM}/Accounts/{compte_id}/Deals",
            params={"fields": "Deal_Name,Pipeline,Stage,Equipement_nature_et_marque", "per_page": 100},
            headers=await _h(),
        )
        if r.status_code == 200:
            for d in r.json().get("data", []):
                if d.get("Pipeline") == PIPELINE_REFI and d.get("Stage") not in STAGES_FINALES:
                    ouverts.append(d)
        mots_equip = [w for w in _mots(equipement, 8)]
        for d in ouverts:
            texte = " ".join(_norm(x) for x in (d.get("Deal_Name", ""), d.get("Equipement_nature_et_marque", "")))
            if any(_norm(w) in texte for w in mots_equip if len(w) >= 4):
                return json.dumps({
                    "statut": "dossier_existant",
                    "deal_id": d.get("id"),
                    "dossier": d.get("Deal_Name"),
                    "compte": compte_nom,
                    "compte_id": compte_id,
                    "etape": d.get("Stage"),
                    "message": "Une affaire Refinancement ouverte existe déjà pour ce client et cet équipement : rien n'a été créé.",
                }, ensure_ascii=False)

        # --- 3. résolution des picklists ---
        stage_api, _ = await _resolve(chttp, "Qualification", "Deals", "Stage", DEALS_REFI_LAYOUT_ID)
        stage_api = stage_api or "Qualification"
        type_api = None
        if type_produit:
            type_api, valeurs = await _resolve(chttp, type_produit, "Deals", "Type_de_produit", DEALS_REFI_LAYOUT_ID)
            if not type_api:
                libelles = "; ".join(f"{lib} (= {act})" for act, lib in valeurs) or "Cession/Location, Location/Cession"
                return json.dumps({"erreur": f"type_produit inconnu : {type_produit}. Valeurs : {libelles}"}, ensure_ascii=False)

        # --- 4. fournisseur (distributeur apporteur) ---
        fournisseur: dict = {}
        warnings: list = []
        if distributeur:
            dist = await _trouver_compte(chttp, distributeur)
            if dist["verdict"] == "unique":
                f = dist["comptes"][0]
                fournisseur = {"id": f.get("id")}
            elif dist["verdict"] == "plusieurs":
                warnings.append("Plusieurs comptes correspondent au distributeur : aucun rattaché (préciser le SIREN).")
            else:
                warnings.append(
                    f"Distributeur « {distributeur} » non trouvé dans le CRM : nom tracé dans la description, "
                    f"fiche compte à créer (le rattacher ensuite comme fournisseur)."
                )

        # --- 5. création de l'affaire ---
        pht, pttc = _montant_float(prix_ht), _montant_float(prix_ttc)
        deal_name = f"{compte_nom} — {(equipement or '')[:60]}".strip()[:120]
        champs: dict = {
            "Layout": {"id": DEALS_REFI_LAYOUT_ID},
            "Deal_Name": deal_name,
            "Pipeline": PIPELINE_REFI,
            "Stage": stage_api,
            "Account_Name": {"id": compte_id},
            "Equipement_nature_et_marque": equipement,
            "Description": _note("", f"demande enregistrée (canal : {source}"
                                     + (f" ; apportée par {distributeur}" if distributeur else "")
                                     + ")" + (f" — {note}" if note else "")),
        }
        if type_api:
            champs["Type_de_produit"] = type_api
        if fournisseur:
            champs["Fournisseur_quipement"] = fournisseur
        if pht is not None:
            champs["Prix_d_acquisition_HT"] = pht
        if pttc is not None:
            champs["Prix_d_acquisition_TTC"] = pttc
        r = await chttp.post(f"{ZOHO_CRM}/Deals", json={"data": [champs]}, headers=await _h())
        if r.status_code >= 400:
            # repli : payload minimal (Layout + nom + pipeline + étape + compte)
            minimal = {k: champs[k] for k in ("Layout", "Deal_Name", "Pipeline", "Stage", "Account_Name")}
            r2 = await chttp.post(f"{ZOHO_CRM}/Deals", json={"data": [minimal]}, headers=await _h())
            if r2.status_code >= 400:
                return json.dumps({"erreur": f"création refusée : {r2.status_code} {r2.text[:300]} | première tentative : {r.text[:200]}"}, ensure_ascii=False)
            r = r2
            warnings.append("Affaire créée en payload MINIMAL (un champ a été refusé) : vérifier en complément à l'étape qualification.")
        data = r.json()["data"][0]
        if data.get("status") != "success":
            return json.dumps({"erreur": data}, ensure_ascii=False)
        deal_id = data["details"]["id"]

        return json.dumps({
            "statut": "cree",
            "deal_id": deal_id,
            "dossier": deal_name,
            "compte": compte_nom,
            "compte_id": compte_id,
            "etape": stage_api,
            "pipeline": PIPELINE_REFI,
            "champs": {k: v for k, v in champs.items() if k not in ("Layout",)},
            "warnings": warnings or None,
            "message": f"Dossier créé (étape {stage_api}). Prochaine étape du process : fiche client (preparer_client), puis qualification.",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Étape 2 — fiche client : création / vérification (bancabilité)
# ---------------------------------------------------------------------------
@agents.tool
async def preparer_client(
    client: str = "",
    siren: str = "",
    ville: str = "",
    scoring: bool = False,
) -> str:
    """PRÉPARE la fiche du bénéficiaire pour un dossier de crédit-bail (étape 2) :
    1. résout le SIREN (donné, ou lu dans le nom ; sinon recherché au registre
       national Pappers par nom de société, filtré par ville si fournie) ;
    2. crée le compte Zoho s'il n'existe pas, SINON complète UNIQUEMENT les champs
       vides (SIRET, adresse de facturation) : jamais d'écrasement des données
       déjà saisies par Olivier ;
    3. renvoie le dossier de bancabilité : forme juridique, ancienneté, effectif,
      NAF, dirigeants (cautions potentielles), finances des 3 derniers exercices
      (CA, résultat net, croissance) et adresse du siège.
    - client : nom de société OU SIREN. - siren : SIREN si déjà connu (9 chiffres,
      zéros de tête inclus, transmis TEL QUEL).
    - scoring : si True, ajoute le scoring Pappers (financier + non financier :
      note A-E, score /20, probabilité de défaillance à 1 an) — COÛTE ~30 CREDITS
      Pappers : ne le mettre que si Olivier le demande explicitement.
    Aucun doublon : le dédoublonnage se fait par SIREN (champ SIRET du compte).
    Renvoie JSON {compte_id, statut_fiche: cree|enrichi|existant, siren, identite,
    dirigeants, adresse, finances, scoring?, fiche_mise_a_jour, points_attention}."""
    siren = (siren or "").strip() or _siren_de(client)
    async with httpx.AsyncClient(timeout=90) as chttp:
        # --- 1. résolution du SIREN ---
        if not siren:
            params = {"q": " ".join(_mots(client, 6)) or client, "per_page": 5, "api_token": os.environ.get("PAPPERS_API_KEY", "")}
            if ville:
                params["ville"] = ville
            try:
                r = await chttp.get(f"{PAPPERS_API}/recherche", params=params)
                resultats = r.json().get("resultats", []) if r.status_code == 200 else []
            except Exception:
                resultats = []
            actifs = [e for e in resultats if (e.get("etat") == "Actif" if isinstance(e.get("etat"), str) else True)]
            if len(actifs) == 1:
                siren = actifs[0].get("siren", "")
            else:
                return json.dumps({
                    "erreur": "SIREN non résolu : candidats à trancher par Olivier (donner le SIREN ou la ville).",
                    "candidats": [
                        {"raison_sociale": e.get("nom_entreprise"), "siren": e.get("siren"),
                         "ville": (e.get("siege") or {}).get("ville"), "etat": e.get("etat")}
                        for e in resultats[:5]
                    ],
                }, ensure_ascii=False)

        # --- 2. compte Zoho existant ? ---
        existant = await _search("Accounts", f"(SIRET:equals:{siren})", "id,Account_Name,SIRET,Billing_Street,Billing_City,Billing_Zip_Code,Billing_State,Description")

        # --- 3. registre national : fiche + finances (+ scoring si demandé) ---
        identite: dict = {}
        dirigeants: list = []
        finances: list = []
        scoring_data = None
        siege: dict = {}
        try:
            params = {"siren": siren, "api_token": os.environ.get("PAPPERS_API_KEY", "")}
            if scoring:
                params["champs_supplementaires"] = "scoring_financier,scoring_non_financier"
            r = await chttp.get(f"{PAPPERS_API}/entreprise", params=params)
            if r.status_code == 200:
                d = r.json() or {}
                identite = {
                    "raison_sociale": d.get("nom_entreprise"),
                    "forme_juridique": d.get("forme_juridique"),
                    "date_creation": d.get("date_creation"),
                    "capital": d.get("capital"),
                    "effectif": (d.get("finances") or [{}])[0].get("effectif") if d.get("finances") else None,
                    "naf": (d.get("libelle_naf") or d.get("naf")),
                    "procedure_collective": d.get("procedure_collective") or None,
                }
                for dirg in (d.get("dirigeants") or []):
                    dirigeants.append({
                        "nom": " ".join(x for x in (dirg.get("prenom"), dirg.get("nom")) if x),
                        "qualite": dirg.get("qualite"),
                        "annee_naissance": dirg.get("annee_de_naissance"),
                    })
                vus: dict = {}
                for f in (d.get("finances") or []):
                    an = f.get("annee")
                    if an is None:
                        continue
                    prev = vus.get(an)
                    if prev is None or (not prev.get("date_de_cloture_exercice") and f.get("date_de_cloture_exercice")):
                        vus[an] = f
                finances = [
                    {"exercice": f.get("date_de_cloture_exercice") or str(f.get("annee") or ""),
                     "ca": f.get("chiffre_affaires"),
                     "resultat_net": f.get("resultat"),
                     "croissance_ca": f.get("taux_croissance_chiffre_affaires")}
                    for f in [vus[a] for a in sorted(vus, reverse=True)][:3]
                ]
                sc = {k: d.get(k) for k in ("scoring_financier", "scoring_non_financier") if d.get(k)}
                scoring_data = sc or None
                siege = d.get("siege") or {}
            elif r.status_code in (402, 403, 429):
                return json.dumps({"erreur": f"Registre national indisponible (HTTP {r.status_code}, crédits Pappers ?) : réessayer plus tard."}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"erreur": f"registre national injoignable : {type(e).__name__}"}, ensure_ascii=False)

        # --- 4. création ou enrichissement (champs vides uniquement) ---
        statut_fiche = "existant"
        maj_champs: list = []
        adresse_siege = siege.get("adresse")
        if not isinstance(adresse_siege, str):
            adresse_siege = siege.get("adresse_ligne_1") if isinstance(siege.get("adresse_ligne_1"), str) else None
        if not existant:
            payload = {
                "data": [{
                    "Account_Name": identite.get("raison_sociale") or client,
                    "SIRET": siren,
                    "Billing_Street": adresse_siege or None,
                    "Billing_City": siege.get("ville") or None,
                    "Billing_Zip_Code": siege.get("code_postal") or None,
                    "Billing_State": siege.get("region") or None,
                    "Description": _note("", "fiche créée par l'agent refinancement pour un dossier de crédit-bail (source : registre national)"),
                }],
                "duplicate_check_fields": ["SIRET"],
            }
            for k in list(payload["data"][0]):
                if k != "duplicate_check_fields" and payload["data"][0][k] is None:
                    del payload["data"][0][k]
            r = await chttp.post(f"{ZOHO_CRM}/Accounts", json=payload, headers=await _h())
            if r.status_code >= 400:
                r = await chttp.post(
                    f"{ZOHO_CRM}/Accounts",
                    json={"data": [{"Account_Name": identite.get("raison_sociale") or client, "SIRET": siren}],
                          "duplicate_check_fields": ["SIRET"]},
                    headers=await _h(),
                )
            if r.status_code >= 400:
                return json.dumps({"erreur": f"création du compte refusée : {r.status_code} {r.text[:200]}"}, ensure_ascii=False)
            data = r.json()["data"][0]
            if data.get("status") != "success":
                return json.dumps({"erreur": data}, ensure_ascii=False)
            compte_id = data.get("details", {}).get("id")
            statut_fiche = "cree"
            maj_champs = ["Account_Name", "SIRET", "adresse (si connue)"]
        else:
            compte = existant[0]
            compte_id = compte.get("id")
            modifs: dict = {"id": compte_id}
            if not (compte.get("SIRET") or "").strip():
                modifs["SIRET"] = siren
                maj_champs.append("SIRET")
            if adresse_siege and not (compte.get("Billing_Street") or "").strip():
                modifs["Billing_Street"] = adresse_siege
                maj_champs.append("rue")
            if siege.get("ville") and not (compte.get("Billing_City") or "").strip():
                modifs["Billing_City"] = siege.get("ville")
                maj_champs.append("ville")
            if siege.get("code_postal") and not (compte.get("Billing_Zip_Code") or "").strip():
                modifs["Billing_Zip_Code"] = siege.get("code_postal")
                maj_champs.append("code postal")
            if len(modifs) > 1:
                desc = await _lire_description(chttp, "Accounts", compte_id)
                modifs["Description"] = _note(desc, f"fiche enrichie pour dossier crédit-bail ({', '.join(maj_champs)} complété(s) via le registre national)")
                r = await chttp.put(f"{ZOHO_CRM}/Accounts", json={"data": [modifs]}, headers=await _h())
                if r.status_code >= 400:
                    return json.dumps({"erreur": f"enrichissement refusé : {r.status_code} {r.text[:200]}"}, ensure_ascii=False)
                statut_fiche = "enrichi"

        # --- 5. points d'attention factuels pour la décision « bankable » ---
        points: list = []
        if identite.get("procedure_collective"):
            points.append(f"procédure collective en cours : {identite.get('procedure_collective')}")
        try:
            dc = identite.get("date_creation")
            if dc:
                age = (datetime.now() - datetime.strptime(dc[:10], "%Y-%m-%d")).days / 365.25
                identite["anciennete_ans"] = round(age, 1)
                if age < 3:
                    points.append(f"jeune entreprise ({round(age, 1)} ans) : assurabilité/préfinancement à vérifier")
        except Exception:
            pass
        resultats_negatifs = [f for f in finances if f.get("resultat_net") is not None and f["resultat_net"] < 0]
        if len(resultats_negatifs) >= 2:
            points.append("résultat net négatif sur plusieurs exercices récents")
        if not finances:
            points.append("comptes non publiés ou absents au registre : finances à obtenir par Olivier (relevés, liasse)")
        if scoring and scoring_data:
            for nom_sc, sc in scoring_data.items():
                if isinstance(sc, dict) and sc.get("note") in ("D", "E"):
                    points.append(f"scoring {nom_sc} faible : note {sc.get('note')} ({sc.get('score')}/20)")

        return json.dumps({
            "compte_id": compte_id,
            "statut_fiche": statut_fiche,
            "siren": siren,
            "identite": identite,
            "dirigeants": dirigeants,
            "adresse": {"rue": adresse_siege, "code_postal": siege.get("code_postal"), "ville": siege.get("ville"), "region": siege.get("region")},
            "finances": finances,
            "scoring": (scoring_data if scoring else None),
            "fiche_mise_a_jour": maj_champs,
            "points_attention": points or None,
            "message": "Dossier de bancabilité prêt. La décision « bankable ou non » reste à Olivier ; les chiffres manquants se demandent au client (relevés bancaires, liasse).",
        }, ensure_ascii=False)
