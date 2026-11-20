"""Outils Zoho CRM de l'agent refinancement (crédit-bail) — SylvaGreen Capital.

Progression : étapes 1-2 outillées ; convention actée « 1 devis = 1 affaire » :
- enregistrer_demande : crée le DOSSIER (Affaire du Pipeline « Refinancement »,
  disposition Refinancement, étape Qualification) dès la prise de demande ; nom
  préfixé du code du dossier {CODE}-{NNN} (index par compte, croissant, jamais
  réutilisé).
- preparer_client    : crée/complète la fiche Compte du bénéficiaire depuis le
  registre national (Pappers) et renvoie le dossier de bancabilité.
- lire_piece_jointe : lit le TEXTE des pièces jointes (synthèses, devis) posées
  sur un compte client ou un dossier du CRM (Zoho ; PDF via pypdf) ; filtre
  non_codes pour ne voir que les devis fournisseur NON traités.
- marquer_devis_traite : renomme le devis brut avec le code du dossier
  ({CODE}-{NNN}_{date}_{nom original}) et le recopie sur l'affaire — un devis
  codé n'est jamais retraité.
- enregistrer_synthese : Note Zoho + fichier SYN-{CODE}_{date}.txt sur l'affaire.
- lire_synthese : relit les synthèses (notes + fichiers SYN-) d'un dossier.
- generer_demande_refinancement : génère la DEMANDE DE REFINANCEMENT
  officielle (PDF charte SylvaGreen, 5 pages + couverture, page de réponse à
  compléter et signer par l'organisme) à destination du (ou des) organisme(s)
  bancaire(s) du panel ; un exemplaire NOMINATIF par organisme ; chaque
  exemplaire est posé sur l'affaire sous DEM-{CODE}_{ORG}_{date}.pdf.
  Les réponses des organismes suivent la convention REP-{CODE}_{ORG}_{date}
  (fichier) + note « Réponse {ORG} ». Les préfixes DEM- et REP- sont traités
  comme des fichiers déjà traités : jamais retraités, jamais renommés.

Les devis fournisseur bruts sont déposés par Olivier sur le COMPTE client
(parking) sous un nom NON codé ; le préfixe SYN- est réservé aux synthèses.
Le module Devis (Quotes) porte les VERSIONS de devis ; il exige des lignes produit
(sous-formulaire Quoted_Items obligatoire) : il est créé au chiffrage, quand
l'équipement réel est connu — jamais à la prise de demande.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import calendar
import unicodedata
from datetime import datetime, timedelta

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


def _closing_date(validite: str) -> tuple:
    """(date_ISO, commentaire) : date de création + durée d'expiration du devis
    fournisseur (« 3 mois », « 4 mois », « 6 semaines », « 90 jours », « 90 » = jours).
    Repli si la durée est absente/inconnue : J+90, avec commentaire explicite."""
    s = (validite or "").strip().lower()
    base = datetime.now()
    m = re.search(r"(\d+)\s*(mois|month)", s)
    if m:
        mois = int(m.group(1))
        total = base.month - 1 + mois
        an, mois_n = base.year + total // 12, total % 12 + 1
        d = base.replace(year=an, month=mois_n, day=min(base.day, calendar.monthrange(an, mois_n)[1]))
        return f"{d:%Y-%m-%d}", f"création + {mois} mois (durée d'expiration du devis)"
    m = re.search(r"(\d+)\s*(sem(?:aine)?s?|week)", s)
    if m:
        jours = int(m.group(1)) * 7
        return f"{base + timedelta(days=jours):%Y-%m-%d}", f"création + {m.group(1)} semaines (durée d'expiration du devis)"
    m = re.search(r"\d+", s)
    if m and int(m.group()) > 0:
        jours = int(m.group())
        return f"{base + timedelta(days=jours):%Y-%m-%d}", f"création + {jours} jours (durée d'expiration du devis)"
    return f"{base + timedelta(days=90):%Y-%m-%d}", "durée d'expiration du devis NON indiquée : J+90 par défaut, à corriger dès que la durée est connue"


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


async def _trouver_deal(client_http, cible: str) -> dict:
    """Localise une affaire par id (>= 15 chiffres) ou nom exact (normalisé).
    Renvoie {verdict: introuvable|unique|plusieurs, deals: [...] }."""
    cible = (cible or "").strip()
    if cible.isdigit() and len(cible) >= 15:
        r = await client_http.get(
            f"{ZOHO_CRM}/Deals", params={"ids": cible, "fields": "id,Deal_Name,Pipeline"}, headers=await _h())
        if r.status_code == 200 and r.json().get("data"):
            return {"verdict": "unique", "deals": r.json()["data"]}
    cn = _norm(cible)
    if not cn:
        return {"verdict": "introuvable", "deals": []}
    mc = re.match(r"^([A-Za-z]{2,4})-?(\d{3})$", cible.strip())
    if mc:
        # recherche par CODE de dossier (ex. « LES-001 ») : pas de mots-clés
        prefixe, num = mc.group(1).upper(), mc.group(2)
        cn_code = _norm(prefixe + "-" + num)
        tout = await _search("Deals", f"(Deal_Name:starts_with:{prefixe})", "id,Deal_Name,Pipeline")
        exacts = [d for d in tout if cn_code in _norm(d.get("Deal_Name") or "")]
        if exacts:
            return {"verdict": "unique", "deals": exacts[:1]}
        return {"verdict": "introuvable", "deals": []}
    preco = _mots(cible, 1)
    tout = await _search("Deals", f"(Deal_Name:starts_with:{preco[0]})", "id,Deal_Name,Pipeline") if preco else []
    exacts = [d for d in tout if _norm(d.get("Deal_Name") or "") == cn]
    if exacts:
        return {"verdict": "unique", "deals": exacts[:1]}
    contiennent = [d for d in tout if cn in _norm(d.get("Deal_Name") or "")]
    if len(contiennent) == 1:
        return {"verdict": "unique", "deals": contiennent}
    return {"verdict": "plusieurs" if contiennent else "introuvable", "deals": contiennent[:5]}


async def _pieces_jointes(client_http, module: str, record_id: str) -> list:
    """Pièces jointes d'un enregistrement (cette org exige le paramètre fields)."""
    r = await client_http.get(
        f"{ZOHO_CRM}/{module}/{record_id}/Attachments",
        params={"fields": "File_Name,Size", "per_page": 50},
        headers=await _h(),
    )
    if r.status_code != 200:
        return []
    return r.json().get("data", [])


def _extrait_texte(nom: str, contenu: bytes) -> str:
    """Texte d'une pièce jointe : PDF (pypdf), texte brut ; sinon message clair."""
    basses = (nom or "").lower()
    if basses.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except Exception:
            return "[PDF : bibliothèque d'extraction indisponible dans le worker]"
        try:
            lecteur = PdfReader(io.BytesIO(contenu))
            pages = [(p.extract_text() or "") for p in lecteur.pages]
            texte = "\n".join(pages)
            if not texte.strip():
                return "[PDF sans couche texte (scan ou image) : non déchiffré, demander la version texte]"
            return texte
        except Exception as e:
            return f"[PDF illisible : {type(e).__name__}]"
    if basses.endswith((".txt", ".md", ".csv", ".json", ".html", ".htm")):
        return contenu.decode("utf-8", "replace")
    return "[fichier non textuel : non déchiffré (images/OCR non gérés)]"


# ---------------------------------------------------------------------------
# Helpers du flux « 1 devis = 1 affaire » (codes, renommage, synthèses)
# ---------------------------------------------------------------------------
def _code_client(nom: str) -> str:
    """Code du compte : 3 premières lettres alphabétiques du nom, majuscules."""
    lettres = "".join(ch for ch in _norm(nom) if ch.isalpha())[:3].upper()
    return lettres if len(lettres) >= 2 else "DOS"


def _est_traite(nom_fichier: str) -> bool:
    """Fichier déjà traité : synthèse (SYN-), demande/réponse organisme
    (DEM-/REP-) ou devis codé (code dossier en tête)."""
    n = (nom_fichier or "").strip()
    if n.upper().startswith(("SYN-", "DEM-", "REP-")):
        return True
    return bool(re.match(r"^[A-Za-z]{2,4}[-_]\d{3}(_|[-.])", n))


def _code_du_deal(deal_name: str) -> str:
    """Extrait {LETTRES}-{NNN} du nom d'une affaire (vide si absent)."""
    m = re.search(r"\b([A-Za-z]{2,4})-(\d{3})\b", deal_name or "")
    return f"{m.group(1).upper()}-{m.group(2)}" if m else ""

async def _deal_par_code_sur_compte(chttp, compte_id: str, code: str):
    """Affaire d'un compte par son code dossier (nom en préfixe). Immédiat via
    les affaires liées au compte, contrairement à l'API search de Zoho dont
    l'index prend plusieurs minutes (recette API : affaire fraîchement créée
    introuvable en search, présente en liste liée)."""
    cn = _norm(code)
    if len(cn) < 5 or not cn[-3:].isdigit():
        return None
    r = await chttp.get(
        f"{ZOHO_CRM}/Accounts/{compte_id}/Deals",
        params={"fields": "id,Deal_Name,Pipeline", "per_page": 100},
        headers=await _h(),
    )
    if r.status_code != 200:
        return None
    for d in r.json().get("data", []):
        if _norm(d.get("Deal_Name") or "").startswith(cn):
            return d
    return None



def _max_index(noms: list, lettres: str) -> int:
    """Plus grand index {LETTRES}-{NNN} trouvé dans des noms (affaires, fichiers)."""
    pat = re.compile(re.escape(lettres) + r"[-_ ]?(\d{3})", re.IGNORECASE)
    maxi = 0
    for nom in noms:
        for m in pat.finditer(nom or ""):
            maxi = max(maxi, int(m.group(1)))
    return maxi


async def _televerser_piece(chttp, module: str, record_id: str, nom: str, contenu: bytes) -> dict:
    """Téléverse une pièce jointe (multipart). Renvoie {id} ou {erreur}."""
    r = await chttp.post(
        f"{ZOHO_CRM}/{module}/{record_id}/Attachments",
        files={"file": (nom, contenu, "application/octet-stream")},
        headers=await _h(),
    )
    if r.status_code >= 400:
        return {"erreur": f"téléversement refusé : HTTP {r.status_code} {r.text[:200]}"}
    d = (r.json().get("data") or [{}])[0]
    if d.get("status") != "success":
        return {"erreur": json.dumps(d, ensure_ascii=False)[:300]}
    return {"id": (d.get("details") or {}).get("id")}


async def _ajouter_note(chttp, module: str, record_id: str, titre: str, texte: str) -> dict:
    """Note rattachée à un enregistrement (Parent_Id.module en objet, validé)."""
    payload: dict = {"Note_Content": texte}
    if titre:
        payload["Note_Title"] = titre
    payload["Parent_Id"] = {"id": record_id, "module": {"api_name": module}}
    payload["Se_Module"] = module
    r = await chttp.post(f"{ZOHO_CRM}/Notes", json={"data": [payload]}, headers=await _h())
    if r.status_code >= 400:
        return {"erreur": f"note refusée : HTTP {r.status_code} {r.text[:200]}"}
    d = (r.json().get("data") or [{}])[0]
    if d.get("code") not in ("SUCCESS", None) and d.get("status") != "success":
        return {"erreur": json.dumps(d, ensure_ascii=False)[:300]}
    return {"id": (d.get("details") or {}).get("id")}


async def _notes_du(chttp, module: str, record_id: str) -> list:
    """Notes d'un enregistrement (liste liée ; retombe sur Note_Content seul)."""
    r = await chttp.get(
        f"{ZOHO_CRM}/{module}/{record_id}/Notes",
        params={"fields": "Note_Title,Note_Content", "per_page": 50},
        headers=await _h(),
    )
    if r.status_code != 200:
        r = await chttp.get(
            f"{ZOHO_CRM}/{module}/{record_id}/Notes",
            params={"fields": "Note_Content", "per_page": 50},
            headers=await _h(),
        )
    return r.json().get("data", []) if r.status_code == 200 else []


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
    validite_devis: str = "",
    code_dossier: str = "",
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
    Nommage : le nom de l'affaire commence par le CODE DU DOSSIER {CODE}-{NNN}
    (3 lettres du compte + index à 3 chiffres, propre au compte, croissant, JAMAIS
    réutilisé, même après une affaire perdue). Règle actée « 1 devis = 1 affaire » :
    une RÉVISION du devis fournisseur = une NOUVELLE affaire à l'index suivant ;
    l'outil ne clôture ni ne modifie JAMAIS une affaire existante (clôture :
    décision d'Olivier seul).
    - code_dossier : code explicite « ABC-005 » (utilisé tel quel), ou « ABC »
      (lettres seules : index automatique), ou vide (code déduit du compte).
    - validite_devis : durée de VALIDITÉ/EXPIRATION du devis fournisseur, lue
      dans le devis (« 3 mois », « 4 mois », « 6 semaines », « 90 jours »...) :
      la Closing_Date de l'affaire = date de création + cette durée (règle
      actée par Olivier). Vide → J+90 par défaut avec avertissement.
    Renvoie JSON {statut: cree, deal_id, dossier, code, index, compte, etape,
    champs, affaires_ouvertes_similaires?, warnings?}."""
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

        # --- 2. code du dossier : {LETTRES}-{NNN} par compte, index jamais réutilisé ---
        code = (code_dossier or "").strip().upper().replace(" ", "").replace("_", "-")
        if code and not re.match(r"^[A-Z]{2,4}(-\d{3})?$", code):
            return json.dumps({"erreur": "code_dossier invalide (attendu : ABC ou ABC-005)."}, ensure_ascii=False)
        lettres = (code.split("-")[0] if code else "") or _code_client(compte_nom)
        noms_deals: list = []
        ouverts: list = []
        r = await chttp.get(
            f"{ZOHO_CRM}/Accounts/{compte_id}/Deals",
            params={"fields": "Deal_Name,Pipeline,Stage,Equipement_nature_et_marque", "per_page": 100},
            headers=await _h(),
        )
        if r.status_code == 200:
            for d in r.json().get("data", []):
                noms_deals.append(d.get("Deal_Name") or "")
                if d.get("Pipeline") == PIPELINE_REFI and d.get("Stage") not in STAGES_FINALES:
                    ouverts.append(d)
        noms_fichiers = [pj.get("File_Name") or "" for pj in await _pieces_jointes(chttp, "Accounts", compte_id)]
        maxi = max(_max_index(noms_deals, lettres), _max_index(noms_fichiers, lettres))
        if code and "-" in code:
            code_final, index = code, int(code.split("-")[1])
            if index <= maxi:
                return json.dumps({"erreur": f"code {code} déjà attribué sur ce compte (dernier index connu : {lettres}-{maxi:03d}). Laisser l'index automatique ou corriger."}, ensure_ascii=False)
        else:
            index = maxi + 1
            code_final = f"{lettres}-{index:03d}"
        # information (jamais bloquant : règle actée 1 devis = 1 affaire)
        similaires = []
        mots_equip = [w for w in _mots(equipement, 8)]
        for d in ouverts:
            texte = " ".join(_norm(x) for x in (d.get("Deal_Name", ""), d.get("Equipement_nature_et_marque", "")))
            if any(_norm(w) in texte for w in mots_equip if len(w) >= 4):
                similaires.append({"deal_id": d.get("id"), "dossier": d.get("Deal_Name"), "etape": d.get("Stage")})

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
        # Closing_Date : OBLIGATOIRE sur la disposition Refinancement (recette
        # API) ; règle actée par Olivier : date de création + durée
        # d'expiration du devis fournisseur.
        closing_date, closing_comment = _closing_date(validite_devis)
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
        warnings.append(f"Closing_Date = {closing_date} ({closing_comment}) ; champ obligatoire du layout Refinancement, à ajuster si l'échéance réelle diffère.")
        if similaires:
            warnings.append(
                "Affaire(s) Refinancement ouverte(s) sur un équipement proche : "
                + " ; ".join(s["dossier"] for s in similaires)
                + ". NOUVELLE affaire créée quand même (règle actée : 1 devis = 1 affaire)."
            )

        # --- 5. création de l'affaire ---
        pht, pttc = _montant_float(prix_ht), _montant_float(prix_ttc)
        deal_name = f"{code_final} — {(equipement or "")[:60] or compte_nom}".strip()[:120]
        champs: dict = {
            "Layout": {"id": DEALS_REFI_LAYOUT_ID},
            "Deal_Name": deal_name,
            "Pipeline": PIPELINE_REFI,
            "Stage": stage_api,
            "Account_Name": {"id": compte_id},
            "Closing_Date": closing_date,
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
            "code": code_final,
            "index": index,
            "compte": compte_nom,
            "compte_id": compte_id,
            "etape": stage_api,
            "pipeline": PIPELINE_REFI,
            "champs": {k: v for k, v in champs.items() if k not in ("Layout",)},
            "affaires_ouvertes_similaires": similaires or None,
            "warnings": warnings or None,
            "message": f"Dossier {code_final} créé (étape {stage_api}). Enchaîner l'étape 1 : marquer_devis_traite (coder le devis brut), puis enregistrer_synthese ; puis fiche client (preparer_client) et qualification.",
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
        existant = await _search("Accounts", f"(SIRET:equals:{siren})", "id,Account_Name,SIRET,Billing_Street,Billing_City,Billing_Code,Billing_State,Description")

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
                    "Billing_Code": siege.get("code_postal") or None,
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
            if siege.get("code_postal") and not (compte.get("Billing_Code") or "").strip():
                modifs["Billing_Code"] = siege.get("code_postal")
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


# ---------------------------------------------------------------------------
# Lecture CRM — pièces jointes (synthèses, devis) d'un compte ou d'un dossier
# ---------------------------------------------------------------------------
@agents.tool
async def lire_piece_jointe(
    client: str = "",
    dossier: str = "",
    nom: str = "",
    non_codes: bool = False,
) -> str:
    """LIT le TEXTE des pièces jointes posées dans le CRM (par Olivier) sur le
    compte d'un client bénéficiaire OU sur son dossier (Affaire) : typiquement
    la synthèse d'une demande ou les devis fournisseur. À utiliser à l'étape 1 :
    si une synthèse est déjà jointe au CRM, lire CETTE source AVANT d'enregistrer
    la demande, et reformuler ce qui a été lu (équipement, prix, distributeur)
    avant toute écriture. Ne JAMAIS déduire un détail non présent dans le texte.
    - client : nom de la société bénéficiaire OU son SIREN (compte Zoho).
    - dossier : nom OU id (17 chiffres) de l'affaire, si la pièce est sur le
      dossier. Si client et dossier sont donnés : pièces des DEUX sources.
      Au moins l'un des deux est requis.
    - nom : sous-chaîne de filtre sur le nom de fichier (ex. « synthèse »,
      « devis », « carbon ») ; vide = toutes les pièces (max 3 par source).
    - non_codes : si True, ne renvoie que les fichiers NON codés (ni devis déjà
      codé {CODE}-{NNN}_..., ni synthèse SYN-) : c'est la vue « devis
      fournisseur à traiter » du parking du compte (max 10 par source). Un
      devis déjà codé = déjà traité : JAMAIS retraité.
    Les fichiers PDF à couche texte et les fichiers texte sont déchiffrés ; les
    images et scans ne le sont pas (message explicite en ce cas).
    Renvoie JSON {sources, pieces: [{sur, nom, taille_octets, texte, tronque}],
    message}, texte éventuellement tronqué à 12 000 caractères par pièce."""
    async with httpx.AsyncClient(timeout=120) as chttp:
        sources: list = []  # (module, record_id, nom_affiché)
        if dossier:
            td = await _trouver_deal(chttp, dossier)
            if td["verdict"] == "unique":
                d = td["deals"][0]
                sources.append(("Deals", str(d.get("id")), d.get("Deal_Name")))
            elif td["verdict"] == "plusieurs":
                return json.dumps({
                    "erreur": "Plusieurs affaires correspondent : préciser l'id du dossier.",
                    "candidats": [{"id": x.get("id"), "nom": x.get("Deal_Name")} for x in td["deals"]],
                }, ensure_ascii=False)
            else:
                return json.dumps({"erreur": f"Dossier introuvable : « {dossier} »."}, ensure_ascii=False)
        if client or not dossier:
            tc = await _trouver_compte(chttp, client)
            if tc["verdict"] == "unique":
                c = tc["comptes"][0]
                sources.append(("Accounts", str(c.get("id")), c.get("Account_Name")))
            elif tc["verdict"] == "plusieurs":
                return json.dumps({
                    "erreur": "Plusieurs comptes correspondent : préciser (SIREN de préférence).",
                    "candidats": [{"id": f.get("id"), "nom": f.get("Account_Name"), "siren": f.get("SIRET")} for f in tc["comptes"]],
                }, ensure_ascii=False)
            elif not sources:
                return json.dumps({"erreur": "Aucune source : donner un client (nom/SIREN) ou un dossier (nom/id)."}, ensure_ascii=False)

        pieces: list = []
        for module, rid, rnom in sources:
            liste_pj = await _pieces_jointes(chttp, module, rid)
            if non_codes:
                liste_pj = [pj for pj in liste_pj if not _est_traite(pj.get("File_Name") or "")]
            for pj in liste_pj[: (10 if non_codes else 3)]:
                fname = pj.get("File_Name") or ""
                if nom and _norm(nom) not in _norm(fname):
                    continue
                entetes = {**await _h(), "Accept": "application/octet-stream"}
                r = await chttp.get(f"{ZOHO_CRM}/{module}/{rid}/Attachments/{pj.get('id')}", headers=entetes)
                if r.status_code != 200:
                    pieces.append({"sur": rnom, "nom": fname, "erreur": f"téléchargement HTTP {r.status_code}"})
                    continue
                texte = _extrait_texte(fname, r.content)
                tronque = False
                if len(texte) > 12000:
                    texte = texte[:12000]
                    tronque = True
                pieces.append({"sur": rnom, "nom": fname, "taille_octets": pj.get("Size"), "texte": texte, "tronque": tronque})

        if not pieces:
            return json.dumps({
                "erreur": "Aucune pièce jointe lisible trouvée (filtre ou source sans pièce).",
                "sources_interrogées": [{"enregistrement": rnom, "module": m} for m, _, rnom in sources],
            }, ensure_ascii=False)
        return json.dumps({
            "sources": [{"enregistrement": rnom, "module": m} for m, _, rnom in sources],
            "pieces": pieces,
            "nb": len(pieces),
            "message": "Texte brut des pièces jointes. Reformule les points clés (équipement, prix, distributeur, conditions) avant d'enregistrer ou de compléter le dossier.",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Étape 1 (suite) — coder le devis traité, synthèse du dossier, relecture
# ---------------------------------------------------------------------------
@agents.tool
async def marquer_devis_traite(
    client: str,
    fichier: str,
    code_dossier: str,
) -> str:
    """MARQUE un devis fournisseur comme TRAITÉ (à enchaîner juste après
    enregistrer_demande) : renomme le devis brut déposé sur le COMPTE du client
    avec le code du dossier — nouveau nom {CODE}-{NNN}_{AAAAMMJJ}_{nom original}
    (ex. LES-001_20261120_devis-carbon-stop.pdf) — puis RECOPIE le devis sous
    ce nom codé sur l'affaire du dossier. C'est ce renommage qui garantit qu'un
    devis n'est JAMAIS traité deux fois (l'agent ne traite que les fichiers
    non codés du compte).
    - client : nom de la société bénéficiaire OU son SIREN (compte Zoho).
    - fichier : nom du devis brut (ou une sous-chaîne distinctive) tel que
      déposé sur le compte.
    - code_dossier : code {LETTRES}-{NNN} renvoyé par enregistrer_demande
      (ex. « LES-001 ») : OBLIGATOIRE, c'est lui qui identifie l'affaire sur
      laquelle recopier le devis.
    Ordre sécurisant : le nouveau fichier est téléversé AVANT la suppression de
    l'original ; en cas d'échec aucun fichier n'est perdu. Un fichier déjà codé
    (ou SYN-) est refusé, jamais retraité.
    Renvoie JSON {statut: marque, fichier_avant, fichier_apres, piece_id,
    recopie_sur: {deal_id, dossier, piece_id} | null, warnings?}."""
    async with httpx.AsyncClient(timeout=120) as chttp:
        trouve = await _trouver_compte(chttp, client)
        if trouve["verdict"] == "introuvable":
            return json.dumps({"erreur": f"Compte introuvable pour « {client} »."}, ensure_ascii=False)
        if trouve["verdict"] == "plusieurs":
            return json.dumps({
                "erreur": "Plusieurs comptes correspondent : préciser (SIREN de préférence).",
                "candidats": [{"id": f.get("id"), "nom": f.get("Account_Name")} for f in trouve["comptes"]],
            }, ensure_ascii=False)
        compte = trouve["comptes"][0]
        compte_id, compte_nom = compte.get("id"), compte.get("Account_Name")

        code_dossier = (code_dossier or "").strip().upper().replace(" ", "")
        if not re.match(r"^[A-Z]{2,4}-\d{3}$", code_dossier):
            return json.dumps({"erreur": f"code_dossier invalide : « {code_dossier} » (attendu : ABC-001)."}, ensure_ascii=False)

        cible = _norm(fichier)
        if not cible:
            return json.dumps({"erreur": "Donner le nom du fichier devis à coder."}, ensure_ascii=False)
        pieces = await _pieces_jointes(chttp, "Accounts", compte_id)
        match = next((pj for pj in pieces if _norm(pj.get("File_Name") or "") == cible), None)
        if not match:
            cands = [pj for pj in pieces
                     if cible in _norm(pj.get("File_Name") or "") and not _est_traite(pj.get("File_Name") or "")]
            if len(cands) == 1:
                match = cands[0]
        if not match:
            return json.dumps({
                "erreur": "Devis non trouvé sur le compte (ou déjà codé/traité : jamais retraité).",
                "client": compte_nom,
                "fichiers_sur_le_compte": [pj.get("File_Name") for pj in pieces] or None,
            }, ensure_ascii=False)
        fname = match.get("File_Name") or ""
        if _est_traite(fname):
            return json.dumps({"erreur": f"« {fname} » est déjà un fichier codé/synthèse : jamais retraité."}, ensure_ascii=False)

        # affaire de recopie (best effort : le renommage a lieu même sans).
        # Search Zoho : index avec retard → d'abord les affaires LIÉES au
        # compte (immédiat), puis repli sur search.
        deal = await _deal_par_code_sur_compte(chttp, compte_id, code_dossier)
        if not deal:
            td = await _trouver_deal(chttp, code_dossier)
            deal = td["deals"][0] if td["verdict"] == "unique" else None
        warnings: list = []
        if not deal:
            warnings.append(f"Affaire {code_dossier} introuvable : devis codé sur le compte mais NON recopié sur une affaire (à vérifier avec Olivier).")

        base, ext = os.path.splitext(fname)
        nouveau_nom = f"{code_dossier}_{datetime.now():%Y%m%d}_{base}{ext}"

        entetes = {**await _h(), "Accept": "application/octet-stream"}
        r = await chttp.get(f"{ZOHO_CRM}/Accounts/{compte_id}/Attachments/{match.get('id')}", headers=entetes)
        if r.status_code != 200:
            return json.dumps({"erreur": f"téléchargement du devis refusé : HTTP {r.status_code}."}, ensure_ascii=False)
        contenu = r.content

        up = await _televerser_piece(chttp, "Accounts", compte_id, nouveau_nom, contenu)
        if up.get("erreur"):
            return json.dumps({"erreur": up["erreur"], "message": "Rien n'a été modifié : l'original est intact."}, ensure_ascii=False)
        nouvel_id = up.get("id")

        recopie = None
        if deal:
            upd = await _televerser_piece(chttp, "Deals", str(deal.get("id")), nouveau_nom, contenu)
            if upd.get("erreur"):
                warnings.append(f"Recopie sur l'affaire impossible : {upd['erreur']}")
            else:
                recopie = {"deal_id": deal.get("id"), "dossier": deal.get("Deal_Name"), "piece_id": upd.get("id")}

        rd = await chttp.delete(f"{ZOHO_CRM}/Accounts/{compte_id}/Attachments/{match.get('id')}", headers=await _h())
        if rd.status_code >= 400:
            warnings.append(f"Original non supprimé (HTTP {rd.status_code}) : deux exemplaires sur le compte, nettoyage manuel.")

        return json.dumps({
            "statut": "marque",
            "fichier_avant": fname,
            "fichier_apres": nouveau_nom,
            "piece_id": nouvel_id,
            "client": compte_nom,
            "compte_id": compte_id,
            "recopie_sur": recopie,
            "warnings": warnings or None,
            "message": f"Devis codé {code_dossier} : il ne sera plus jamais traité."
                       + (" Recopié sur l'affaire du dossier." if recopie else ""),
        }, ensure_ascii=False)


@agents.tool
async def enregistrer_synthese(dossier: str, texte: str, client: str = "") -> str:
    """ENREGISTRE la synthèse du dossier (dernier geste de l'étape 1) : pose sur
    l'AFFAIRE un double enregistrement lisible — une Note Zoho (titre
    « Synthèse {code} ») ET un fichier texte SYN-{CODE}_{AAAAMMJJ}.txt — pour que
    la lecture du devis reste consultable sans rouvrir le PDF. Convention : le
    préfixe SYN- est RÉSERVÉ aux synthèses, jamais traitées comme des devis.
    - dossier : nom OU id (17 chiffres) OU code (ex. « LES-001 ») de l'affaire.
      CONSEILLÉ : donner AUSSI le client si le dossier vient d'être créé (la
      recherche Zoho indexe les affaires avec un retard de plusieurs minutes ;
      le client permet la résolution immédiate par les affaires liées).
    - client : nom OU SIREN du compte bénéficiaire (recommandé juste après la
      création de l'affaire).
    - texte : synthèse factuelle de la lecture du devis (équipement, prix,
      distributeur, conditions, version) — UNIQUEMENT ce qui a été lu dans la
      pièce, rien d'inventé. Une ligne d'en-tête [SYN-...] est ajoutée
      automatiquement. Un texte trop long est refusé.
    Renvoie JSON {statut: cree, dossier, deal_id, code, note_id, fichier,
    fichier_id}."""
    texte = (texte or "").strip()
    if not texte:
        return json.dumps({"erreur": "Texte de synthèse vide."}, ensure_ascii=False)
    if len(texte) > 20000:
        return json.dumps({"erreur": f"Synthèse trop longue ({len(texte)} caractères, max 20 000) : la résumer."}, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=90) as chttp:
        deal = None
        if client:
            tc = await _trouver_compte(chttp, client)
            if tc["verdict"] == "unique":
                deal = await _deal_par_code_sur_compte(chttp, tc["comptes"][0].get("id"), dossier)
            elif tc["verdict"] == "plusieurs":
                return json.dumps({
                    "erreur": "Plusieurs comptes correspondent : préciser (SIREN de préférence).",
                    "candidats": [{"id": f.get("id"), "nom": f.get("Account_Name")} for f in tc["comptes"]],
                }, ensure_ascii=False)
        td = ({"verdict": "unique", "deals": [deal]} if deal else None) or await _trouver_deal(chttp, dossier)
        if td["verdict"] == "plusieurs":
            return json.dumps({
                "erreur": "Plusieurs affaires correspondent : préciser l'id ou le code du dossier.",
                "candidats": [{"id": d.get("id"), "nom": d.get("Deal_Name")} for d in td["deals"]],
            }, ensure_ascii=False)
        if td["verdict"] != "unique":
            return json.dumps({"erreur": f"Dossier introuvable : « {dossier} ». Donner aussi le client (résolution immédiate via les affaires liées au compte)."}, ensure_ascii=False)
        deal = td["deals"][0]
        deal_id, deal_nom = str(deal.get("id")), deal.get("Deal_Name")
        code = _code_du_deal(deal_nom)
        entete = f"[SYN-{code or 'dossier'} — synthèse agent refinancement, {datetime.now():%d/%m/%Y}]\n"
        note = await _ajouter_note(chttp, "Deals", deal_id, f"Synthèse {code}" if code else "Synthèse", entete + texte)
        if note.get("erreur"):
            return json.dumps({"erreur": note["erreur"]}, ensure_ascii=False)

        base_nom = f"SYN-{code}_{datetime.now():%Y%m%d}" if code else f"SYN-dossier_{datetime.now():%Y%m%d}"
        existants = {pj.get("File_Name") for pj in await _pieces_jointes(chttp, "Deals", deal_id)}
        nom_fichier, n = f"{base_nom}.txt", 2
        while nom_fichier in existants:
            nom_fichier = f"{base_nom}-{n}.txt"
            n += 1
        up = await _televerser_piece(chttp, "Deals", deal_id, nom_fichier, (entete + texte).encode("utf-8"))
        fichier_id = up.get("id")
        if up.get("erreur"):
            return json.dumps({
                "statut": "cree",
                "dossier": deal_nom,
                "deal_id": deal_id,
                "code": code or None,
                "note_id": note.get("id"),
                "fichier": None,
                "warning": f"Note créée mais fichier texte refusé : {up['erreur']}",
            }, ensure_ascii=False)

        return json.dumps({
            "statut": "cree",
            "dossier": deal_nom,
            "deal_id": deal_id,
            "code": code or None,
            "note_id": note.get("id"),
            "fichier": nom_fichier,
            "fichier_id": fichier_id,
            "message": "Synthèse enregistrée (note + fichier texte) sur l'affaire. Étape 1 terminée pour ce devis.",
        }, ensure_ascii=False)


@agents.tool
async def lire_synthese(dossier: str = "", client: str = "") -> str:
    """RELIT les synthèses disponibles sans rouvrir les PDF : notes « Synthèse »
    et fichiers SYN-... d'un dossier (affaire), et/ou fichiers SYN-... posés
    sur le compte d'un client (parking, ex. synthèse d'origine).
    - dossier : nom OU id OU code (ex. « LES-001 ») de l'affaire. Pour une
      affaire fraîchement créée, donner AUSSI le client (recherche Zoho indexée
      avec un retard : le client permet la résolution immédiate).
    - client : nom OU SIREN du compte. Au moins l'un des deux est requis ;
      si les deux : synthèses de l'affaire PUIS celles posées sur le compte.
    Renvoie JSON {sources, notes: [{titre, contenu}], fichiers: [{sur, nom,
    texte}], message} ; erreur claire si aucune synthèse trouvée."""
    async with httpx.AsyncClient(timeout=90) as chttp:
        sources: list = []  # (module, record_id, nom_affiché)
        if dossier:
            # résolution immédiate par le compte quand il est donné (search Zoho :
            # index avec retard), sinon par l'API search (id, nom, code).
            d_trouve = None
            if client:
                tc0 = await _trouver_compte(chttp, client)
                if tc0["verdict"] == "unique":
                    d_trouve = await _deal_par_code_sur_compte(chttp, tc0["comptes"][0].get("id"), dossier)
            if not d_trouve:
                td = await _trouver_deal(chttp, dossier)
                if td["verdict"] == "unique":
                    d_trouve = td["deals"][0]
                elif td["verdict"] == "plusieurs":
                    return json.dumps({
                        "erreur": "Plusieurs affaires correspondent : préciser l'id ou le code du dossier.",
                        "candidats": [{"id": x.get("id"), "nom": x.get("Deal_Name")} for x in td["deals"]],
                    }, ensure_ascii=False)
            if d_trouve:
                sources.append(("Deals", str(d_trouve.get("id")), d_trouve.get("Deal_Name")))
            elif not client:
                return json.dumps({"erreur": f"Dossier introuvable : « {dossier} » (indiquer aussi le client : la recherche Zoho indexe les affaires fraîches avec un retard)." }, ensure_ascii=False)
        if client or not dossier:
            tc = await _trouver_compte(chttp, client)
            if tc["verdict"] == "unique":
                c = tc["comptes"][0]
                sources.append(("Accounts", str(c.get("id")), c.get("Account_Name")))
            elif tc["verdict"] == "plusieurs" and not sources:
                return json.dumps({
                    "erreur": "Plusieurs comptes correspondent : préciser (SIREN de préférence).",
                    "candidats": [{"id": f.get("id"), "nom": f.get("Account_Name")} for f in tc["comptes"]],
                }, ensure_ascii=False)
            elif tc["verdict"] != "unique" and not sources:
                return json.dumps({"erreur": "Aucune source : donner un dossier (nom/id/code) ou un client (nom/SIREN)."}, ensure_ascii=False)

        notes: list = []
        fichiers: list = []
        for module, rid, rnom in sources:
            for n in await _notes_du(chttp, module, rid):
                titre = (n.get("Note_Title") or "")
                contenu = (n.get("Note_Content") or "")
                if "synth" in _norm(titre) or contenu.lstrip().startswith("[SYN"):
                    notes.append({"sur": rnom, "titre": titre or None, "contenu": contenu})
            for pj in await _pieces_jointes(chttp, module, rid):
                fname = pj.get("File_Name") or ""
                if not fname.upper().startswith("SYN-"):
                    continue
                entetes = {**await _h(), "Accept": "application/octet-stream"}
                r = await chttp.get(f"{ZOHO_CRM}/{module}/{rid}/Attachments/{pj.get('id')}", headers=entetes)
                if r.status_code != 200:
                    fichiers.append({"sur": rnom, "nom": fname, "erreur": f"téléchargement HTTP {r.status_code}"})
                    continue
                fichiers.append({"sur": rnom, "nom": fname, "texte": _extrait_texte(fname, r.content)})

        if not notes and not fichiers:
            return json.dumps({
                "erreur": "Aucune synthèse trouvée (ni note, ni fichier SYN-).",
                "sources_interrogées": [{"enregistrement": rnom, "module": m} for m, _, rnom in sources],
            }, ensure_ascii=False)
        return json.dumps({
            "sources": [{"enregistrement": rnom, "module": m} for m, _, rnom in sources],
            "notes": notes or None,
            "fichiers": fichiers or None,
            "message": "Synthèses lues : s'appuyer uniquement sur ce contenu, sans rien inventer au-delà.",
        }, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Étape 3 — demande de refinancement officielle (nominative par organisme)
# ---------------------------------------------------------------------------
from html import escape as _esc

EMETTEUR = {
    # Émetteur du contrat de demande de refinancement. SylvaGreen Conseils
    # aujourd'hui ; basculer vers SylvaGreen Management (holding) quand elle
    # aura son SIRET : ne changer QUE ce dictionnaire.
    "nom": "SylvaGreen Conseils",
    "nom_maj": "SYLVAGREEN CONSEILS",
    "representant": "M. Olivier PELLEGRIN",
    "fonction": "gérant",
    "adresse": "14, Le Sirieix\u00a0— 19220 Bassignac-le-Haut",
    "tel": "06\u00a017\u00a079\u00a039\u00a008",
    "email": "olivier.pellegrin@sylvagreen.consulting",
    "siret": "450 479 407 00055",
}

CHAMP_ORGANISMES = "Organismes_sollicit_s"

_REPO_FILES: dict = {}


def _fichier_repo(nom: str) -> str:
    """Fichier livré avec le worker (template, logo), lu une seule fois."""
    if nom not in _REPO_FILES:
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), nom)
        with open(chemin, "r", encoding="utf-8") as f:
            _REPO_FILES[nom] = f.read()
    return _REPO_FILES[nom]


def _fmt_eur(x, prefix: str = "") -> str:
    """714203 -> « 714 203 € » (séparateur insécable). None -> « — »."""
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return _esc(str(x))
    s = f"{v:,.0f}".replace(",", "\u00a0")
    return f"{prefix}{s}\u00a0€"


def _fmt_signe_pct(x) -> str:
    """20.8 -> « +20,8 % » (le registre exprime la croissance en pourcentage)."""
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return _esc(str(x))
    s = f"{v:+,.1f}".replace(",", "\u00a0").replace(".", ",")
    return f"{s}\u00a0%"


def _fmt_pct(x) -> str:
    if x is None:
        return "—"
    return ("%.1f" % x).replace(".", ",") + "\u00a0%"


def _annee_de(exercice) -> str:
    """« 31/12/2025 » ou 2025 -> « 2025 » (dernier groupe de 4 chiffres)."""
    m = re.search(r"(\d{4})", str(exercice or ""))
    return m.group(1) if m else str(exercice or "—")


def _siren_affiche(siren: str) -> str:
    s = (siren or "").strip()
    return f"{s[0:3]}\u00a0{s[3:6]}\u00a0{s[6:9]}" if len(s) == 9 else _esc(s or "—")


def _date_fr(v) -> str:
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return _esc(str(v)) if v else "—"


def _mn_pc(rn, ca) -> float | None:
    """Marge nette en % (None si données absentes ou CA nul)."""
    try:
        return rn / ca * 100 if (rn is not None and ca) else None
    except (TypeError, ZeroDivisionError):
        return None


async def _fiche_pappers(chttp, siren: str) -> dict:
    """Fiche registre national : identité, dirigeants, finances (3 derniers
    exercices), siège. Lève RuntimeError avec un message clair si indisponible."""
    r = await chttp.get(f"{PAPPERS_API}/entreprise", params={"siren": siren, "api_token": os.environ.get("PAPPERS_API_KEY", "")})
    if r.status_code in (402, 403, 429):
        raise RuntimeError(f"registre national indisponible (HTTP {r.status_code}, crédits Pappers ?) : réessayer plus tard")
    if r.status_code != 200:
        raise RuntimeError(f"registre national : HTTP {r.status_code}")
    d = r.json() or {}
    identite = {
        "raison_sociale": d.get("nom_entreprise"),
        "forme_juridique": d.get("forme_juridique"),
        "date_creation": d.get("date_creation"),
        "capital": d.get("capital"),
        "effectif": (d.get("finances") or [{}])[0].get("effectif") if d.get("finances") else None,
        "naf": (d.get("libelle_naf") or d.get("naf")),
    }
    dirigeants = [
        {"nom": " ".join(x for x in (dg.get("prenom"), dg.get("nom")) if x), "qualite": dg.get("qualite")}
        for dg in (d.get("dirigeants") or [])
    ]
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
         "annee": _annee_de(f.get("annee") or f.get("date_de_cloture_exercice")),
         "ca": f.get("chiffre_affaires"),
         "resultat_net": f.get("resultat"),
         "croissance_ca": f.get("taux_croissance_chiffre_affaires")}
        for f in [vus[a] for a in sorted(vus, reverse=True)][:3]
    ]
    return {"identite": identite, "dirigeants": dirigeants, "finances": finances, "siege": d.get("siege") or {}}


def _code_organisme(description: str) -> str | None:
    """Code organisme extrait de la Description d'une fiche Refinanceur.
    Les fiches réelles écrivent « Code demande (DEM-LES-001-001_CAL_) : CAL. » :
    le séparateur « : » n'arrive qu'APRÈS la parenthèse, d'où la fenêtre
    intermédiaire [^:\\n]{0,60} entre « Code demande » et les deux-points.
    Les fiches plus anciennes « Code demande : CAL » restent lues."""
    m = re.search(r"code\s+demande[^:\n]{0,60}:\s*([A-Za-z]{2,5})\b", description or "", re.IGNORECASE)
    return m.group(1).upper() if m else None


async def _organismes_panel(chttp) -> dict:
    """Panel bancaire : fiches Comptes « Refinanceur », code extrait de la
    Description (voir _code_organisme). Recettes : la recherche Zoho indexe
    les fiches avec un retard de plusieurs minutes, mais le critère search
    (Account_Type:equals:Refinanceur) reste fiable ; l'org dépasse 200
    comptes donc un GET liste unique peut tronquer : on tente le search en
    priorité, avec repli sur la liste complète PAGINÉE filtrée côté client."""
    champs = "Account_Name,Account_Type,Description,Billing_City"
    panel: dict = {}

    def _absorbe(fiches) -> None:
        for c in fiches or []:
            if _norm(c.get("Account_Type") or "") != "refinanceur":
                continue
            code = _code_organisme(c.get("Description") or "")
            if not code or code in panel:
                continue
            panel[code] = {
                "id": str(c.get("id")),
                "nom": c.get("Account_Name") or "",
                "ville": c.get("Billing_City") or "",
            }

    # 1) search par Account_Type : fiable même avec plus de 200 comptes
    try:
        r = await chttp.get(
            f"{ZOHO_CRM}/Accounts/search",
            params={"criteria": "(Account_Type:equals:Refinanceur)",
                    "fields": champs, "per_page": 100},
            headers=await _h(),
        )
        if r.status_code == 200:
            _absorbe(r.json().get("data", []))
    except Exception:
        pass

    # 2) repli : liste complète paginée, filtrée côté client
    if not panel:
        page = 1
        while page <= 5:
            r = await chttp.get(
                f"{ZOHO_CRM}/Accounts",
                params={"fields": champs, "per_page": 200, "page": page},
                headers=await _h(),
            )
            if r.status_code != 200:
                break
            j = r.json()
            _absorbe(j.get("data", []))
            if not (j.get("info") or {}).get("more_records"):
                break
            page += 1

    if not panel:
        raise RuntimeError(
            "aucune fiche Compte « Refinanceur » avec un code demande lisible "
            "dans la Description (format attendu « Code demande (DEM-…_ORG_) : CODE »)"
        )
    return panel


def _label_organisme(nom_fiche: str, labels: list) -> str | None:
    """Libellé picklist « Organismes sollicités » correspondant au nom de la
    fiche du panel (mots communs, insensible aux accents). None si aucun."""
    tokens = [_norm(t) for t in re.split(r"[^a-zA-Z0-9]+", nom_fiche or "") if len(t) >= 4]
    meilleur, score = None, 0
    for lab in labels:
        nl = _norm(lab)
        s = sum(1 for t in tokens if t and t in nl)
        if s > score:
            meilleur, score = lab, s
    return meilleur if score >= 2 else None


async def _maj_organismes_sollicites(chttp, deal_id: str, labels: list) -> bool:
    """Écrit le multiselect Organismes_sollicit_s (fusion des valeurs déjà
    présentes), puis VÉRIFIE en relecture (recette : Zoho répond SUCCESS
    silencieusement même sans écrire). Renvoie True si confirmé."""
    r = await chttp.get(
        f"{ZOHO_CRM}/Deals",
        params={"ids": deal_id, "fields": CHAMP_ORGANISMES},
        headers=await _h(),
    )
    actuel: list = []
    if r.status_code == 200:
        v = (r.json().get("data") or [{}])[0].get(CHAMP_ORGANISMES)
        if isinstance(v, str):
            actuel = [x for x in v.split(";") if x]
        elif isinstance(v, list):
            actuel = list(v)
    fusion = list(dict.fromkeys([a for a in actuel if a] + labels))
    for candidat in (fusion, ";".join(fusion)):
        r = await chttp.put(
            f"{ZOHO_CRM}/Deals",
            json={"data": [{"id": deal_id, CHAMP_ORGANISMES: candidat}]},
            headers=await _h(),
        )
        if r.status_code >= 400:
            continue
        rr = await chttp.get(
            f"{ZOHO_CRM}/Deals",
            params={"ids": deal_id, "fields": CHAMP_ORGANISMES},
            headers=await _h(),
        )
        if rr.status_code != 200:
            continue
        v = (rr.json().get("data") or [{}])[0].get(CHAMP_ORGANISMES)
        lus = v.split(";") if isinstance(v, str) else (v or [])
        if lus and set(labels) <= {str(x) for x in lus}:
            return True
    return False


def _pdf_depuis_html(html_final: str) -> bytes:
    """PDF A4 via WeasyPrint (import paresseux : la lib n'est pas requise pour
    compiler le module hors worker)."""
    import io as _io

    from weasyprint import HTML

    buf = _io.BytesIO()
    HTML(string=html_final).write_pdf(buf)
    return buf.getvalue()


def _table_grille(montant: float, durees: list, apports: list, vide: bool) -> str:
    """Grille de taux apport × durée. vide=True : page de réponse (cellules
    vides) ; vide=False : corps de la demande (taux à compléter « ……… % »)."""
    ths = "".join(f'<th class="num">{d}\u00a0mois ({d // 12}\u00a0ans)</th>' for d in durees)
    rows = []
    for p in apports:
        lib = "Sans apport (0\u00a0%)" if p == 0 else f"Apport {p}\u00a0% ({_fmt_eur(round(montant * p / 100))})"
        motif = "&nbsp;" if vide else "…………&nbsp;%"
        cells = "".join(f'<td class="num a-remplir">{motif}</td>' for _ in durees)
        rows.append(f"<tr><td>{lib}</td>{cells}</tr>")
    return f'<table><tr><th style="width:34%">Apport en comptant</th>{ths}</tr>{"".join(rows)}</table>'


def _page1_body(code, societe, nom_org, date_fr, validite, fins, devis_cites, ident) -> str:
    annees = [f["annee"] for f in fins]
    if fins:
        src_fin = f"Les données financières proviennent des comptes publiés ({'exercices ' + annees[-1] + ' à ' + annees[0] if len(annees) > 1 else 'exercice ' + annees[0]})"
    else:
        src_fin = "Les données financières seront transmises sur demande (liasses fiscales)"
    src_devis = "des devis fournisseurs cités au chapitre 4" if devis_cites else f"du dossier {code} (CRM de {EMETTEUR['nom']})"
    return f"""
    <h2>1. Objet de la demande</h2>
    <p class="sous-chap">Une demande officielle de financement en location/cession, adressée nominativement à l'organisme destinataire</p>
    <div class="encadre">La présente demande de refinancement est adressée par {EMETTEUR['nom']}, apporteur de l'opération, au nom de la société {_esc(societe)}, bénéficiaire du financement, à l'attention de {_esc(nom_org)}, organisme destinataire du présent exemplaire, en vue de la location, avec cession finale, de l'équipement décrit au chapitre 4. L'organisme destinataire est invité à y répondre par oui ou par non, au moyen de la page de signature du présent document, en proposant et en garantissant un taux nominal annuel selon la grille du chapitre 5.</div>
    <p>Le montage retenu est la location/cession&nbsp;: l'organisme acquiert l'équipement neuf auprès du fournisseur, le loue à la société pendant la durée du contrat, puis lui cède l'équipement au terme du contrat, à sa valeur vénale. L'opération est portée par SylvaGreen, spécialiste du financement durable des entreprises de la filière bois.</p>
    <p class="aise">La présente demande contractualise un engagement. Le projet décrit aux chapitres 3 et 4 est d'ores et déjà engagé&nbsp;: la société a signé un mandat de recherche de financement, et l'opération sera conduite avec l'organisme dont la proposition sera retenue. Un exemplaire nominatif du présent document est adressé à chaque organisme consulté&nbsp;; chacun répond sur l'exemplaire reçu, en son nom propre. La réponse, taux compris, engage l'organisme qui la signe, pendant une durée de {validite} jours.</p>
    <p>Ce document est rattaché au dossier {_esc(code)} et reflète la situation de la société à la date du {_esc(date_fr)}. {src_fin}&nbsp;; les données techniques proviennent {src_devis}.</p>
    <p class="sources">Sources&nbsp;: comptes publiés de la société (registre national des entreprises), devis fournisseurs cités, CRM de {EMETTEUR['nom']}.</p>

    <h2>2. La société en un coup d'œil</h2>
    <p class="sous-chap">Fiche d'identité au {_esc(date_fr)}</p>
    <table>
      <tr><th style="width:30%">Dénomination</th><td>{_esc(societe)}</td></tr>
      <tr><th>Forme juridique</th><td>{ident['forme']}</td></tr>
      <tr><th>Immatriculation</th><td>SIREN {ident['siren']}</td></tr>
      <tr><th>Siège social</th><td>{ident['siege']}</td></tr>
      <tr><th>Date de création</th><td>{ident['cree_le']}</td></tr>
      <tr><th>Capital social</th><td>{ident['capital']}</td></tr>
      <tr><th>Activité (NAF)</th><td>{ident['naf']}</td></tr>
      <tr><th>Effectif</th><td>{ident['effectif']}</td></tr>
      <tr><th>Dirigeants</th><td>{ident['dirigeants']}</td></tr>
    </table>
    <p class="sources">Sources&nbsp;: registre national des entreprises, CRM de {EMETTEUR['nom']}. Mention «&nbsp;—&nbsp;»&nbsp;: donnée non disponible au registre à ce jour.</p>
"""


def _page2_body(fins) -> str:
    if not fins:
        return """
    <h2>3. Lecture financière</h2>
    <p class="sous-chap">Trois exercices publiés, une trajectoire lisible</p>
    <p>Les comptes annuels de la société n'ont pas pu être lus au registre national des entreprises à la date d'établissement de la demande. Les liasses fiscales des derniers exercices seront transmises à l'organisme sur simple demande.</p>
"""
    tetes = "".join(f'<th class="num">Exercice {f["annee"]}</th>' for f in reversed(fins))

    def ligne(libelle: str, fmt) -> str:
        cells = "".join(
            f'<td class="num">{fmt(f) if fmt(f) is not None else "—"}</td>' for f in reversed(fins)
        )
        return f'<tr><td style="width:34%">{libelle}</td>{cells}</tr>'

    ligne_mn = ligne("Marge nette (résultat net / CA)", lambda f: _fmt_pct(_mn_pc(f.get("resultat_net"), f.get("ca"))))
    dernier = fins[0]
    analyse = ""
    if len(fins) > 1 and fins[1].get("ca") and dernier.get("ca"):
        analyse = f"Sur l'exercice {dernier['annee']}, le chiffre d'affaires s'établit à {_fmt_eur(dernier['ca'])}, contre {_fmt_eur(fins[1]['ca'])} sur l'exercice {fins[1]['annee']}"
        if dernier.get("croissance_ca") is not None:
            analyse += f" ({_fmt_signe_pct(dernier['croissance_ca'])})"
        analyse += "."
    else:
        analyse = f"L'exercice {dernier['annee']} publie un chiffre d'affaires de {_fmt_eur(dernier.get('ca'))}."
    if dernier.get("resultat_net") is not None:
        analyse += f" Le résultat net s'établit à {_fmt_eur(dernier['resultat_net'])}"
        mn0 = _mn_pc(dernier.get("resultat_net"), dernier.get("ca"))
        if mn0 is not None:
            analyse += f", soit une marge nette de {_fmt_pct(mn0).replace(chr(160), ' ').strip()}"
        analyse += "."
    mn0 = _mn_pc(dernier.get("resultat_net"), dernier.get("ca"))
    mn1 = _mn_pc(fins[1].get("resultat_net"), fins[1].get("ca")) if len(fins) > 1 else None
    croiss = dernier.get("croissance_ca")
    if (dernier.get("resultat_net") or 0) <= 0:
        encadre = f"Le dernier exercice publie un résultat net de {_fmt_eur(dernier['resultat_net'])}&nbsp;: l'organisme pourra demander les explications et les perspectives correspondantes&nbsp;; les liasses et les relevés seront fournis sur demande."
    elif croiss is not None and float(croiss) >= 10 and (mn1 is None or mn0 is None or mn0 >= mn1 - 1):
        encadre = "Une croissance à deux chiffres qui ne dégrade pas la rentabilité&nbsp;: le profil correspond au critère attendu d'un financement locatif d'équipement productif, l'équipement refinancé venant renforcer l'outil de production de la société."
    elif croiss is not None and float(croiss) >= 10 and mn1 is not None and mn0 is not None:
        encadre = f"Une croissance à deux chiffres ({_fmt_signe_pct(croiss)})&nbsp;; la marge nette recule toutefois de {('%.1f' % (mn1 - mn0)).replace('.', ',')} points, à {_fmt_pct(mn0).replace(chr(160), ' ').strip()}&nbsp;: les explications seront fournies sur demande."
    else:
        encadre = f"Un chiffre d'affaires de {_fmt_eur(dernier.get('ca'))} et un résultat net de {_fmt_eur(dernier.get('resultat_net'))} sur le dernier exercice&nbsp;: le profil correspond à celui attendu d'un financement locatif d'équipement productif."
    annees = [f["annee"] for f in fins]
    liste = f"exercices {annees[-1]} à {annees[0]}" if len(annees) > 1 else f"exercice {annees[0]}"
    ligne_ca = ligne("Chiffre d'affaires", lambda f: _fmt_eur(f.get("ca")))
    ligne_cr = ligne("Croissance du chiffre d'affaires", lambda f: _fmt_signe_pct(f.get("croissance_ca")))
    ligne_rn = ligne("Résultat net", lambda f: _fmt_eur(f.get("resultat_net")))
    return f"""
    <h2>3. Lecture financière</h2>
    <p class="sous-chap">Trois exercices publiés, une trajectoire lisible</p>
    <table>
      <tr><th style="width:34%">Indicateur</th>{tetes}</tr>
      {ligne_ca}
      {ligne_cr}
      {ligne_rn}
      {ligne_mn}
    </table>
    <p>{analyse}</p>
    <div class="encadre">{encadre}</div>
    <p>Les liasses fiscales complètes des {liste} sont disponibles sur demande, ainsi que le détail de l'endettement existant et des cautions éventuelles.</p>
    <p class="sources">Sources&nbsp;: comptes publiés de la société ({liste}).</p>
"""


def _page3_body(code, date_fr, validite, ctx) -> str:
    """Chapitres 4 (projet d'équipement) et 5 (grille de refinancement).
    ctx : equipement, fournisseur, devis_principal, devis_option, livraison,
    montant, montant_sans_option, durees, apports."""
    m = ctx["montant"]
    grille = _table_grille(m, ctx["durees"], ctx["apports"], vide=False)
    sans_option = ""
    if ctx["montant_sans_option"]:
        sans_option = f" Sur la base du seul périmètre principal, le montant s'établit à {_fmt_eur(ctx['montant_sans_option'])} HT&nbsp;; le montant définitif sera ajusté au périmètre confirmé."
    grille_intro = f"<p><strong>Montant global à financer&nbsp;: {_fmt_eur(m)} HT</strong>, hors transport et travaux connexes non inclus.{sans_option} La sécurisation du paiement fournisseur est prévue (acompte contre caution d'acompte, solde au chargement contre lettre de crédit irrévocable confirmée). Le contrat prendra la forme d'une location de l'équipement au profit de la société, avec cession de l'équipement à la société au terme du contrat, à sa valeur vénale.</p>"
    durees_annees = " ou ".join(f"{d // 12}\u00a0ans" for d in ctx["durees"]) if ctx["durees"] else ""
    return f"""
    <h2>4. Le projet d'équipement</h2>
    <p class="sous-chap">Un équipement neuf, apporté par un distributeur partenaire</p>
    <table>
      <tr><th style="width:26%">Équipement</th><td>{ctx['equipement']}</td></tr>
      <tr><th>Fournisseur</th><td>{ctx['fournisseur']}</td></tr>
      <tr><th>Devis principal</th><td>{ctx['devis_principal']}</td></tr>
      <tr><th>Option</th><td>{ctx['devis_option']}</td></tr>
      <tr><th>Livraison / mise en service</th><td>{ctx['livraison']}</td></tr>
    </table>
    <div class="encadre">Un actif neuf, adossé à un besoin industriel identifié. Équipement neuf acquis sur facture du fournisseur, puis loué à la société&nbsp;: le montage de location/cession avec cession finale à la valeur vénale au terme s'applique directement.</div>
    <p class="sources">Sources&nbsp;: devis fournisseurs cités ci-dessus, dossier {_esc(code)} (CRM de {EMETTEUR['nom']}).</p>

    <h2>5. La grille de refinancement</h2>
    <p class="sous-chap">Un montant, des durées ({durees_annees}), une grille de taux à compléter par l'organisme</p>
    {grille_intro}
    <p>L'organisme est invité à renseigner le taux nominal annuel qu'il accorde, pour chaque combinaison d'apport en comptant et de durée listée ci-dessous. La grille complétée, datée et signée vaut proposition ferme, garantie {validite} jours.</p>
    {grille}
    <p>Merci de préciser, le cas échéant, les frais de dossier, un taux indexé (nominal et indice de référence) et les conditions d'assurance de l'équipement. La grille figure également en page de réponse, destinée à être complétée et signée.</p>
    <p class="sources">Sources&nbsp;: devis cités au chapitre 4.</p>
"""


def _page4_body(code, societe, fins, ctx) -> str:
    """Chapitres 6 (pièces disponibles) et 7 (synthèse). ctx : date_fr, delai,
    montant, durees, apports."""
    date_fr = ctx["date_fr"]
    delai = ctx["delai"]
    montant, durees, apports = ctx["montant"], ctx["durees"], ctx["apports"]
    if fins:
        annees = [f["annee"] for f in fins]
        liste_ex = f"exercices {annees[-1]} à {annees[0]}" if len(annees) > 1 else f"exercice {annees[0]}"
        liasses = f"liasses fiscales des {liste_ex}"
    else:
        liasses = "liasses fiscales des derniers exercices"
    durees_annees = " ou ".join(f"{d // 12}\u00a0ans" for d in durees)
    apports_txt = "avec ou sans apport en comptant" if len(apports) > 1 else ("avec apport en comptant" if apports and apports[0] > 0 else "sans apport en comptant")
    dernier = fins[0] if fins else None
    perf = ""
    if dernier and dernier.get("croissance_ca") is not None:
        try:
            if float(dernier["croissance_ca"]) >= 10:
                perf = f"en croissance de plus de {int(float(dernier['croissance_ca']))} % sur son dernier exercice{' et profitable' if (dernier.get('resultat_net') or 0) > 0 else ''}, "
            elif (dernier.get("resultat_net") or 0) > 0:
                perf = f"profitable sur son dernier exercice (résultat net de {_fmt_eur(dernier['resultat_net'])}), "
        except (TypeError, ValueError):
            perf = ""
    elif dernier and (dernier.get("resultat_net") or 0) > 0:
        perf = f"profitable sur son dernier exercice (résultat net de {_fmt_eur(dernier['resultat_net'])}), "
    return f"""
    <h2>6. Cadre de l'opération et pièces disponibles</h2>
    <p class="sous-chap">Un dossier complet, disponible immédiatement sur demande</p>
    <p>Les pièces suivantes sont prêtes à être transmises&nbsp;: extrait Kbis de moins de 3 mois, {liasses}, devis détaillés des équipements, mandat de recherche de financement signé par la société, plaquette de présentation de SylvaGreen.</p>
    <p>Les points suivants seront complétés au fil de la qualification, sans retarder la réponse à la demande&nbsp;: plan de financement définitif, date de mise en service, cautions et garanties éventuellement demandées.</p>
    <p class="sources">Sources&nbsp;: dossier {_esc(code)}, CRM de {EMETTEUR['nom']}.</p>

    <h2>7. Synthèse</h2>
    <p class="sous-chap">Un engagement réciproque, une réponse attendue sous {delai} jours</p>
    <p>La société {_esc(societe)}, {perf}recherche le financement en location/cession de l'équipement décrit au chapitre 4 pour {_fmt_eur(montant)} HT, sur une durée de {durees_annees}, {apports_txt}. La société s'engage à conduire l'opération avec l'organisme dont la proposition sera retenue.</p>
    <p>Nous demandons à votre organisme&nbsp;: une réponse par oui ou par non, au moyen de la page ci-après&nbsp;; en cas d'accord, les taux nominaux annuels garantis selon la grille du chapitre 5, les éventuels frais de dossier, et leur durée de validité. Un retour sous {delai} jours permettrait de présenter le montage à la société dans les meilleures conditions. Les propositions des organismes non retenus demeureront strictement confidentielles.</p>
    <p class="aise">{EMETTEUR['nom']} est l'interlocuteur unique du dossier et transmet toutes les pièces complémentaires sur simple demande&nbsp;: {EMETTEUR['representant']}, {EMETTEUR['tel']}, {EMETTEUR['email']}.</p>
    <p class="mention">Document d'information établi par {EMETTEUR['nom']} à partir de données publiques et des devis fournisseurs cités. Il ne constitue ni une offre de crédit, ni un conseil en investissement. Les chiffres de la société proviennent de ses comptes publiés&nbsp;; les montants du projet, des devis fournisseurs cités.</p>
"""


def _page5_body(code, nom_org, validite, ctx) -> str:
    """Chapitre 8 : page de réponse de l'organisme (à compléter, dater, signer)."""
    grille = _table_grille(ctx["montant"], ctx["durees"], ctx["apports"], vide=True)
    return f"""
    <h2>8. Réponse de l'organisme de financement</h2>
    <p class="sous-chap">Page à compléter, dater et signer par l'organisme sollicité, en réponse à la demande de refinancement du dossier {_esc(code)}</p>
    <table>
      <tr><th style="width:30%">Organisme</th><td>{_esc(nom_org)}</td></tr>
      <tr><th>Établissement / agence</th><td class="a-remplir">&nbsp;</td></tr>
      <tr><th>Correspondant</th><td class="a-remplir">&nbsp;</td></tr>
      <tr><th>Date de la réponse</th><td class="a-remplir">&nbsp;</td></tr>
    </table>
    <p style="margin-top:2.5mm"><strong>Réponse (cocher la case applicable)</strong></p>
    <p class="a-remplir"><span class="case"></span>&nbsp;&nbsp;<strong>La demande est acceptée.</strong> L'organisme propose et garantit les taux nominaux annuels ci-dessous pour le dossier {_esc(code)}, pendant une durée de {validite} jours à compter de la date de la réponse. Cette proposition vaut engagement de financer l'affaire aux conditions indiquées, sous réserve des vérifications d'usage.</p>
    {grille}
    <table>
      <tr><th style="width:30%">Frais de dossier</th><td class="a-remplir">&nbsp;</td></tr>
      <tr><th>Taux indexé (nominal et indice)</th><td class="a-remplir">&nbsp;</td></tr>
      <tr><th>Conditions d'assurance demandées</th><td class="a-remplir">&nbsp;</td></tr>
    </table>
    <p class="a-remplir"><span class="case"></span>&nbsp;&nbsp;<strong>La demande n'est pas acceptée.</strong> motif principal&nbsp;: …………………………………………………………………………</p>
    <p style="margin-top:4.5mm">Fait à …………………………, le …………………………</p>
    <p style="margin-top:3mm">Lu et approuvé, bon pour accord sur les taux indiqués ci-dessus,</p>
    <p>Nom et qualité du représentant habilité&nbsp;: ………………………………………………</p>
    <p>Signature et cachet de l'organisme&nbsp;:</p>
    <p style="height:22mm; border:1px solid #b9c7b2; margin-top:2.5mm; border-radius:2px;">&nbsp;</p>
    <p class="mention" style="margin-top:4mm">Les propositions des organismes non retenus demeurent strictement confidentielles et ne donnent lieu à aucune exploitation. La présente page, signée, sera versée au dossier {_esc(code)} tenu par {EMETTEUR['nom']}.</p>
"""


@agents.tool
async def generer_demande_refinancement(
    dossier: str,
    organismes: list,
    montant_global_ht: float,
    montant_sans_option_ht: float | None = None,
    equipement: str = "",
    fournisseur: str = "",
    devis_principal: str = "",
    devis_option: str = "",
    livraison: str = "",
    delai_jours: int = 15,
    validite_jours: int = 60,
    durees: list | None = None,
    apports: list | None = None,
) -> str:
    """GÉNÈRE la demande de refinancement officielle du dossier (étape 3) : un
    PDF charte SylvaGreen (couverture + 5 pages, page 5 à compléter, dater et
    SIGNER par l'organisme) qui contractualise la demande — la banque répond
    par oui ou par non et garantit ses taux selon la grille. UN exemplaire
    NOMINATIF par organisme, posé sur l'AFFAIRE sous DEM-{CODE}_{ORG}_{date}.pdf
    (+ une note par organisme) ; le champ « Organismes sollicités » de l'affaire
    est mis à jour et vérifié en relecture.
    La banque AVANT le devis client : cet outil est appelé avant tout chiffrage.
    Produit UNIQUE de ce flux : la LOCATION/CESSION (matériel neuf acheté pour
    le client, loué au client, cession au client à sa valeur vénale au terme).
    - dossier : nom OU id OU code (ex. « LES-001 ») de l'affaire.
    - organismes : codes du panel bancaire, ex. ["CAL"] ou ["CAL","BNP","SGEF"].
    - montant_global_ht : montant global à financer, OBLIGATOIRE (le lire dans la
      synthèse du dossier, jamais l'inventer) ; montant_sans_option_ht : montant
      du périmètre principal seul, si distinct.
    - equipement / fournisseur / devis_principal / devis_option / livraison :
      descriptions FACTUELLES (lues dans les devis ou la synthèse) ; laissées
      vides, elles sont lues dans l'affaire CRM, sinon « — ».
    - delai_jours (15 par défaut) : retour souhaité ; validite_jours (60 par
      défaut) : durée de garantie des taux proposés.
    - durees (défaut [60,72,84] mois) et apports (défaut [0,10,20,30] %).
    JAMAIS remplacer un exemplaire existant (erreur explicite si le fichier
    existe déjà). L'agent ne choisit JAMAIS l'organisme retenu : seule la
    réponse signée d'Olivier fait foi (champ Financeur_organismes).
    Renvoie JSON {statut, dossier, deal_id, code, fichiers[], organismes[],
    warnings[], message}."""
    warnings: list = []
    # --- validation des entrées ---
    try:
        montant = round(float(montant_global_ht), 2)
    except (TypeError, ValueError):
        return json.dumps({"erreur": "montant_global_ht invalide : donner le montant global à financer en euros HT (le lire dans la synthèse du dossier)."}, ensure_ascii=False)
    if montant <= 0:
        return json.dumps({"erreur": "montant_global_ht doit être positif."}, ensure_ascii=False)
    if isinstance(organismes, str):
        organismes = [organismes]
    codes_org = [str(c).strip().upper() for c in (organismes or []) if str(c).strip()]
    if not codes_org:
        return json.dumps({"erreur": "organismes manquants : donner les codes du panel bancaire (ex. [\"CAL\"])."}, ensure_ascii=False)
    try:
        liste_durees = sorted({int(d) for d in (durees or [60, 72, 84]) if int(d) > 0})
        liste_apports = sorted({int(a) for a in (apports or [0, 10, 20, 30]) if 0 <= int(a) < 100})
    except (TypeError, ValueError):
        return json.dumps({"erreur": "durees/apports invalides : listes d'entiers."}, ensure_ascii=False)
    if not liste_durees or not liste_apports:
        return json.dumps({"erreur": "durees/apports vides."}, ensure_ascii=False)
    validite = int(validite_jours or 60)
    delai = int(delai_jours or 15)
    date_fr = datetime.now().strftime("%d/%m/%Y")
    date_iso = datetime.now().strftime("%Y%m%d")

    async with httpx.AsyncClient(timeout=120) as chttp:
        # --- 1. résolution de l'affaire et lectures CRM ---
        td = await _trouver_deal(chttp, dossier)
        if td["verdict"] == "plusieurs":
            return json.dumps({
                "erreur": "Plusieurs affaires correspondent : préciser l'id ou le code du dossier.",
                "candidats": [{"id": d.get("id"), "nom": d.get("Deal_Name")} for d in td["deals"]],
            }, ensure_ascii=False)
        if td["verdict"] != "unique":
            return json.dumps({"erreur": f"Dossier introuvable : « {dossier} ». Donner le code (ex. LES-001) ou l'id."}, ensure_ascii=False)
        deal = td["deals"][0]
        deal_id = str(deal["id"])
        r = await chttp.get(
            f"{ZOHO_CRM}/Deals",
            params={"ids": deal_id,
                    "fields": "id,Deal_Name,Account_Name,Equipement_nature_et_marque,Fournisseur_quipement,Prix_d_acquisition_HT," + CHAMP_ORGANISMES},
            headers=await _h(),
        )
        if r.status_code != 200 or not r.json().get("data"):
            return json.dumps({"erreur": f"lecture de l'affaire refusée : HTTP {r.status_code}."}, ensure_ascii=False)
        d0 = r.json()["data"][0]
        deal_nom = d0.get("Deal_Name") or ""
        code = _code_du_deal(deal_nom) or _code_du_deal(dossier)
        if not code:
            return json.dumps({"erreur": "Le dossier n'a pas de code {CODE}-{NNN} (convention « 1 devis = 1 affaire ») : passer par l'étape 1."}, ensure_ascii=False)

        compte_obj = d0.get("Account_Name") or {}
        compte_id = str((compte_obj.get("id") if isinstance(compte_obj, dict) else compte_obj) or "")
        compte_nom = (compte_obj.get("name") if isinstance(compte_obj, dict) else "") or ""
        compte: dict = {}
        if compte_id:
            rc = await chttp.get(
                f"{ZOHO_CRM}/Accounts",
                params={"ids": compte_id, "fields": "Account_Name,SIRET,Billing_Street,Billing_City,Billing_Code,Billing_State"},
                headers=await _h(),
            )
            if rc.status_code == 200 and rc.json().get("data"):
                compte = rc.json()["data"][0]
        societe = (compte.get("Account_Name") or compte_nom or deal_nom).strip()
        siren = _siren_de(societe) or (compte.get("SIRET") or "")[:9]

        # --- 2. fiche registre national (identité + finances) ---
        fiche = None
        if len(siren or "") == 9:
            try:
                fiche = await _fiche_pappers(chttp, siren)
            except RuntimeError as e:
                warnings.append(str(e))
        else:
            warnings.append("SIREN absent du compte : fiche d'identité incomplète dans le document ; passer preparer_client d'abord.")
        fins = (fiche or {}).get("finances") or []
        identite = (fiche or {}).get("identite") or {}
        dirigeants = (fiche or {}).get("dirigeants") or []

        siege_pap = (fiche or {}).get("siege") or {}
        rue = ((compte.get("Billing_Street") or "") or (siege_pap.get("adresse") or "")).strip()
        cp = ((compte.get("Billing_Code") or "") or (siege_pap.get("code_postal") or "")).strip()
        ville = ((compte.get("Billing_City") or "") or (siege_pap.get("ville") or "")).strip()
        region = (compte.get("Billing_State") or "").strip()
        cp_ville = " ".join(x for x in (cp, ville) if x)
        siege_mor = ", ".join(x for x in (rue, cp_ville, region) if x)
        ident = {
            "forme": _esc(identite.get("forme_juridique") or "—"),
            "siren": _siren_affiche(siren),
            "siege": _esc(siege_mor or "—"),
            "cree_le": _date_fr(identite.get("date_creation") or "—"),
            "capital": _fmt_eur(identite.get("capital")),
            "naf": _esc(identite.get("naf") or "—"),
            "effectif": _esc(str(identite["effectif"])) if identite.get("effectif") is not None else "—",
            "dirigeants": _esc(" ; ".join(f"{dg['nom']} ({dg['qualite']})" for dg in dirigeants if dg.get("nom"))) if dirigeants else "non publiés au registre à ce jour, à obtenir de la société",
        }

        # --- 3. panel bancaire et organismes demandés ---
        try:
            panel = await _organismes_panel(chttp)
        except RuntimeError as e:
            return json.dumps({"erreur": f"panel bancaire illisible : {e}"}, ensure_ascii=False)
        inconnus = [c for c in codes_org if c not in panel]
        if inconnus:
            return json.dumps({
                "erreur": f"Code(s) organisme inconnu(s) : {', '.join(inconnus)}.",
                "codes_disponibles": {c: v["nom"] for c, v in sorted(panel.items())},
            }, ensure_ascii=False)

        # --- 4. contexte ch. 4 (factuel uniquement) ---
        fourn_obj = d0.get("Fournisseur_quipement")
        fournisseur_txt = (fournisseur or "").strip() or (fourn_obj.get("name") if isinstance(fourn_obj, dict) else "") or ""
        equip_txt = (equipement or "").strip() or (d0.get("Equipement_nature_et_marque") or "")
        devis_cites = bool((devis_principal or "").strip() or (devis_option or "").strip())
        ctx = {
            "date_fr": date_fr,
            "delai": delai,
            "montant": montant,
            "montant_sans_option": montant_sans_option_ht,
            "durees": liste_durees,
            "apports": liste_apports,
            "equipement": _esc(equip_txt) or "—",
            "fournisseur": _esc(fournisseur_txt) or "—",
            "devis_principal": _esc((devis_principal or "").strip()) or "— (devis joints au dossier)",
            "devis_option": _esc((devis_option or "").strip()) or "—",
            "livraison": _esc((livraison or "").strip()) or "à préciser avec le client",
            "devis_cites": devis_cites,
        }

        # --- 5. génération d'un exemplaire NOMINATIF par organisme ---
        template = _fichier_repo("template_demande.html")
        logo_b64 = "".join(_fichier_repo("logo_base64.txt").split())
        existants = {pj.get("File_Name") for pj in await _pieces_jointes(chttp, "Deals", deal_id)}
        fichiers, orgs_resultat = [], []
        for cd in codes_org:
            fiche_org = panel[cd]
            nom_org = fiche_org["nom"] or cd
            couv = [f"<strong>{_esc(nom_org).upper()}</strong><br/>", "organisme de financement<br/>"]
            if fiche_org.get("ville"):
                couv.append(f"{_esc(fiche_org['ville'])}<br/>")
            couv.append('<span class="role">«&nbsp;le Destinataire&nbsp;»</span>')
            communs = {
                "LOGO": logo_b64,
                "DATE_FR": date_fr,
                "DOC_ORG": _esc(nom_org),
                "CODE": _esc(code),
                "DOSSIER_LIB": _esc(societe),
                "EM_NOM": EMETTEUR["nom"],
                "EM_NOM_MAJ": EMETTEUR["nom_maj"],
                "EM_REPRESENTANT": f"{EMETTEUR['representant']}, {EMETTEUR['fonction']}",
                "EM_ADRESSE": EMETTEUR["adresse"],
                "EM_TEL": EMETTEUR["tel"],
                "EM_EMAIL": EMETTEUR["email"],
                "EM_SIRET": EMETTEUR["siret"],
                "COUV_ORG": "".join(couv),
            }
            pages = [
                _page1_body(code, societe, nom_org, date_fr, validite, fins, devis_cites, ident),
                _page2_body(fins),
                _page3_body(code, date_fr, validite, ctx),
                _page4_body(code, societe, fins, ctx),
                _page5_body(code, nom_org, validite, ctx),
            ]
            html_final = template
            for k, v in communs.items():
                html_final = html_final.replace("{{" + k + "}}", v)
            for i, body in enumerate(pages, 1):
                html_final = html_final.replace("{{PAGE%d_BODY}}" % i, body)
            if "{{" in html_final:
                warnings.append("patron incomplètement rempli : au moins un jeton est resté en l'état")

            nom_pdf = f"DEM-{code}_{cd}_{date_iso}.pdf"
            if nom_pdf in existants:
                return json.dumps({
                    "erreur": f"Un exemplaire pour {cd} existe déjà ({nom_pdf}) : JAMAIS remplacé. Si Olivier veut le refaire, il supprime ou archive d'abord la pièce.",
                    "statut": "refusé",
                    "dossier": deal_nom,
                }, ensure_ascii=False)
            try:
                pdf = _pdf_depuis_html(html_final)
            except Exception as e:
                return json.dumps({"erreur": f"échec de génération du PDF ({type(e).__name__}) : {str(e)[:200]}"}, ensure_ascii=False)
            up = await _televerser_piece(chttp, "Deals", deal_id, nom_pdf, pdf)
            if up.get("erreur"):
                return json.dumps({"erreur": f"exemplaire {cd} refusé : {up['erreur']}"}, ensure_ascii=False)
            note_txt = (
                f"[Demande de refinancement générée le {date_fr}]\n"
                f"Organisme : {nom_org} ({cd})\n"
                f"Montant global à financer : {_fmt_eur(montant).replace(chr(160), ' ')} HT\n"
                f"Durées consultées : {', '.join(str(x) for x in liste_durees)} mois ; apports : {', '.join(str(x) for x in liste_apports)} %\n"
                f"Réponse attendue : grille de taux à compléter et signer (page 5), garantie {validite} jours ; retour souhaité sous {delai} jours.\n"
                f"Exemplaire : {nom_pdf}"
            )
            note = await _ajouter_note(chttp, "Deals", deal_id, f"Demande {cd}", note_txt)
            if note.get("erreur"):
                warnings.append(f"note {cd} refusée : {note['erreur']}")
            existants.add(nom_pdf)
            fichiers.append({"organisme": cd, "fichier": nom_pdf, "attachment_id": up.get("id"), "note_id": note.get("id")})
            orgs_resultat.append({"code": cd, "organisme": nom_org})

        # --- 6. champ « Organismes sollicités » (multiselect, fusion + vérif) ---
        picklist = await _picklist(chttp, "Deals", CHAMP_ORGANISMES, DEALS_REFI_LAYOUT_ID)
        labels = [lib for _, lib in picklist]
        manquants, labels_ok = [], []
        for cd in codes_org:
            lab = _label_organisme(panel[cd]["nom"], labels)
            if lab:
                labels_ok.append(lab)
            else:
                manquants.append(f"{cd} ({panel[cd]['nom']})")
        champ_ok = False
        if labels_ok:
            champ_ok = await _maj_organismes_sollicites(chttp, deal_id, labels_ok)
            if not champ_ok:
                warnings.append("champ Organismes_sollicit_s : écriture non confirmée en relecture (vérifier les libellés picklist avec Olivier)")
        for lab in manquants:
            warnings.append(f"libellé picklist introuvable pour {lab} : champ non mis à jour pour cet organisme")

        return json.dumps({
            "statut": "cree",
            "dossier": deal_nom,
            "deal_id": deal_id,
            "code": code,
            "compte": societe,
            "fichiers": fichiers,
            "organismes": orgs_resultat,
            "montant_global_ht": montant,
            "durees_mois": liste_durees,
            "apports_pct": liste_apports,
            "champ_organismes_sollicites": champ_ok,
            "warnings": warnings or None,
            "message": ("Demande(s) de refinancement générée(s) et jointe(s) à l'affaire, une par organisme. "
                        "Prochaine étape : Olivier envoie chaque exemplaire nominatif à son organisme ; "
                        "la réponse signée revient dans l'affaire (convention REP-{CODE}_{ORG}_{date}). "
                        "L'agent ne choisit jamais l'organisme retenu."),
        }, ensure_ascii=False)
