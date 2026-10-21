# Workflow "dd-overview-ingest" — SylvaGreen Capital
# Pipeline : API Pappers (v2) -> documents markdown -> bibliothèque DD (ex. "BB BGI").
# Secrets requis (Secrets Manager du workspace, liés au déploiement) :
#   - PAPPERS_API_KEY : clé API de votre compte Pappers (onglet API de pappers.fr)
#   - MISTRAL_API_KEY : injectée automatiquement par la plateforme (nom réservé)

import io
import os
from typing import Optional

import httpx
import mistralai.workflows as workflows
from mistralai.client import Mistral
from mistralai.client.models import File
from pydantic import BaseModel

PAPPERS_BASE = "https://api.pappers.fr/v2"

# Bibliothèque "BB BGI" (cible courante du fonds). Surchargable à l'exécution.
BIBLIOTHEQUE_BGI = "01a15e8f-6c68-76d9-8045-251210a64aef"


class DDIngestInput(BaseModel):
    """Entrée du workflow : un SIREN, ou un nom d'entreprise à résoudre."""

    siren: Optional[str] = None
    nom_entreprise: Optional[str] = None
    library_id: str = BIBLIOTHEQUE_BGI


def _headers_pappers() -> dict:
    return {"api-key": os.environ["PAPPERS_API_KEY"]}


@workflows.activity()
async def resoudre_siren(nom_entreprise: str) -> dict:
    """Recherche l'entreprise par nom et renvoie le premier résultat (SIREN)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{PAPPERS_BASE}/recherche",
            params={"q": nom_entreprise},
            headers=_headers_pappers(),
        )
        r.raise_for_status()
        resultats = r.json().get("resultats", [])
        if not resultats:
            raise ValueError(f"Aucune entreprise trouvée pour « {nom_entreprise} »")
        return resultats[0]


@workflows.activity()
async def fetch_fiche(siren: str) -> dict:
    """Récupère la fiche complète Pappers de l'entreprise (identité, finances, dirigeants, BODACC...)."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{PAPPERS_BASE}/entreprise",
            params={"siren": siren},
            headers=_headers_pappers(),
        )
        r.raise_for_status()
        return r.json()


@workflows.activity()
async def fetch_scoring(siren: str) -> dict:
    """Récupère le score de solvabilité Pappers (F4). Echoue proprement si l'offre scoring n'est pas active."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{PAPPERS_BASE}/scoring",
            params={"siren": siren},
            headers=_headers_pappers(),
        )
        if r.status_code in (402, 403, 404):
            return {"disponible": False, "raison": f"HTTP {r.status_code}"}
        r.raise_for_status()
        return {"disponible": True, **r.json()}


def _fmt_montant(v: Optional[float]) -> str:
    if v is None:
        return "n/d"
    return f"{v:,.0f} €".replace(",", " ")


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "n/d"
    return f"{v * 100:.1f} %"


def generer_documents(fiche: dict, scoring: dict) -> list:
    """Transforme la fiche Pappers + scoring en documents markdown pour la bibliothèque DD.

    Aligné sur le canvas « DD Overview — Check-up Express » : identité (TRV),
    synthèse financière (FIN), gouvernance & contentieux (JUR), et données
    brutes pour la traçabilité (ANX).
    """
    nom = fiche.get("denomination") or fiche.get("nom_entreprise") or "Cible"
    siren = fiche.get("siren", "")

    # --- Document 1 : fiche identité (TRV) ---
    dirigeants = fiche.get("dirigeants", []) or []
    lignes_dirigeants = "\n".join(
        f"- **{d.get('nom', '')} {d.get('prenom', '')}** — {d.get('fonction', 'fonction n/d')} "
        f"(depuis {d.get('date_debut', 'n/d')}, {len(d.get('societes', []) or [])} autres sociétés)"
        for d in dirigeants
    ) or "- n/d"

    doc_identite = f"""# Fiche identité — {nom}

> Source : API Pappers (données INSEE / INPI / greffes) — collecte automatisée par l'agent DD Ingest.
> SIREN : {siren}

## Identité légale

| Champ | Valeur |
|---|---|
| Dénomination | {nom} |
| Forme juridique | {fiche.get("forme_juridique", "n/d")} |
| Date de création | {fiche.get("date_creation_formate", fiche.get("date_creation", "n/d"))} |
| Capital social | {_fmt_montant(fiche.get("capital_social"))} |
| Code NAF / activité | {fiche.get("libelle_code_naf", fiche.get("code_naf", "n/d"))} |
| Statut | {fiche.get("etat_entreprise", "n/d")} |
| Effectif (tranche INSEE) | {fiche.get("tranche_effectif", "n/d")} |
| Siège | {fiche.get("adresse_ligne_1", "")} {fiche.get("adresse_ligne_2", "")} {fiche.get("code_postal", "")} {fiche.get("ville", "")} |

## Dirigeants

{lignes_dirigeants}

## Établissements

Nombre d'établissements actifs : {len([e for e in (fiche.get("etablissements") or []) if e.get("etat_administratif") == "Actif"])}
"""

    # --- Document 2 : synthèse financière (FIN) ---
    finances = fiche.get("finances", []) or []
    finances_triees = sorted(finances, key=lambda f: f.get("annee", 0), reverse=True)[:3]
    lignes_fin = "\n".join(
        f"| {f.get('annee', 'n/d')} | {_fmt_montant(f.get('ca'))} | {_fmt_montant(f.get('resultat_net'))} "
        f"| {_fmt_montant(f.get('resultat_excedent'))} | {_fmt_montant(f.get('total_actif'))} "
        f"| {f.get('effectif', 'n/d')} |"
        for f in finances_triees
    ) or "| n/d | n/d | n/d | n/d | n/d | n/d |"

    ca_annees = {f.get("annee"): f.get("ca") for f in finances_triees if f.get("ca") is not None}
    croissance = "n/d (données insuffisantes)"
    if len(ca_annees) >= 2:
        annees = sorted(ca_annees)
        ancien, recent = annees[0], annees[-1]
        if ca_annees[ancien]:
            taux = (ca_annees[recent] - ca_annees[ancien]) / ca_annees[ancien]
            croissance = f"{_fmt_pct(taux)} entre {ancien} et {recent}"

    rentabilite = "n/d"
    derniere = finances_triees[0] if finances_triees else {}
    if derniere.get("ca") and derniere.get("resultat_net") is not None:
        rentabilite = _fmt_pct(derniere["resultat_net"] / derniere["ca"]) + " (résultat net / CA, dernier exercice)"

    if scoring.get("disponible"):
        bloc_score = f"""## Score de solvabilité Pappers (F4)

- **Note** : {scoring.get("note", "n/d")} / 20
- **Probabilité de défaut à 12 mois** : {scoring.get("probabilite_defaillance", "n/d")}
- **Algorithme** : {scoring.get("algorithme", "n/d")}
- Détail : voir données brutes (ANX-00)
"""
    else:
        bloc_score = """## Score de solvabilité Pappers (F4)

- Non disponible sur cette clé API ({raison}).
- En attente : score Infolegale (négociation en cours) ou activation de l'offre scoring Pappers.
""".replace("{raison}", scoring.get("raison", "n/d"))

    doc_finances = f"""# Synthèse financière — {nom}

> Source : API Pappers — collecte automatisée (agent DD Ingest). SIREN : {siren}

## 3 derniers exercices publiés

| Année | CA | Résultat net | Excédent brut d'exploitation | Total actif | Effectif |
|---|---|---|---|---|---|
{lignes_fin}

## Indicateurs calculés (grille Overview)

- **Croissance du CA (F2)** : {croissance}
- **Rentabilité (F3)** : {rentabilite}
- **Comptes déposés (F1)** : {len(finances)} exercices trouvés sur la fiche

{bloc_score}

*Notes : montants bruts tels que déposés aux greffes ; vérifier la confidentialité éventuelle des comptes.*
"""

    # --- Document 3 : gouvernance & contentieux (JUR) ---
    procedures = fiche.get("procedures_collectives", []) or []
    lignes_proc = "\n".join(
        f"- {p.get('type_procedure', 'Procédure')} — {p.get('date_immatriculation', p.get('date', 'n/d'))}"
        for p in procedures
    ) or "- Aucune procédure collective trouvée sur la fiche"

    beneficiaires = fiche.get("beneficiaires_collectifs", []) or []
    lignes_benef = "\n".join(
        f"- {b.get('nom', '')} {b.get('prenom', '')} — {b.get('type', 'n/d')} "
        f"({b.get('part_directe', '')}{b.get('part_indirecte', '')})"
        for b in beneficiaires
    ) or "- Bénéficiaires effectifs : non listés sur la fiche"

    doc_juridique = f"""# Gouvernance & contentieux — {nom}

> Source : API Pappers — collecte automatisée (agent DD Ingest). SIREN : {siren}

## Procédures collectives (J2)

{lignes_proc}

## Bénéficiaires effectifs

{lignes_benef}

## Alertes automatiques

- Procédures collectives actives : **{len(procedures)}**
- Contrôles : à compléter avec BODACC et contentieux (voir check-up express)

*Croiser avec Pascovi / Infolegale pour le détail des contentieux non collectifs.*
"""

    # --- Document 4 : données brutes (ANX, traçabilité) ---
    import json

    doc_brut = (
        f"# Données brutes Pappers — {nom}\n\n"
        f"> Capture JSON de la fiche API (SIREN {siren}) pour traçabilité et re-traitements.\n\n"
        f"```json\n{json.dumps(fiche, ensure_ascii=False, indent=1, default=str)[:90000]}\n```\n"
    )

    return [
        {"filename": f"TRV-01 fiche identite - {nom}.md", "contenu": doc_identite},
        {"filename": f"FIN-01 synthese financiere - {nom}.md", "contenu": doc_finances},
        {"filename": f"JUR-01 gouvernance et procedures - {nom}.md", "contenu": doc_juridique},
        {"filename": f"ANX-00 donnees brutes Pappers - {nom}.md", "contenu": doc_brut},
    ]


@workflows.activity()
async def upload_vers_bibliotheque(library_id: str, documents: list) -> list:
    """Dépose chaque document markdown dans la bibliothèque DD (API Mistral Libraries)."""
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    deposes = []
    for doc in documents:
        fichier = File(
            fileName=doc["filename"],
            content=io.BytesIO(doc["contenu"].encode("utf-8")),
        )
        resultat = client.beta.libraries.documents.upload(library_id=library_id, file=fichier)
        deposes.append(
            {"nom": doc["filename"], "id": getattr(resultat, "id", None)}
        )
    return deposes


@workflows.workflow.define(
    name="dd-overview-ingest",
    workflow_display_name="DD Overview Ingest",
    workflow_description=(
        "Collecte les données publiques Pappers d'une société cible "
        "(identité, finances, dirigeants, procédures, score de solvabilité) "
        "et alimente automatiquement la bibliothèque DD du fonds."
    ),
)
class DDOverviewIngestWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: DDIngestInput) -> dict:
        # 1. Identifier la cible
        if input.siren:
            siren = input.siren
        elif input.nom_entreprise:
            trouve = await resoudre_siren(input.nom_entreprise)
            siren = trouve["siren"]
        else:
            raise ValueError("Fournir 'siren' ou 'nom_entreprise' en entrée.")

        # 2. Collecter fiche + scoring en parallèle
        fiche, scoring = await workflows.gather(
            fetch_fiche(siren),
            fetch_scoring(siren),
        )

        # 3. Générer les documents (fonction pure, déterministe)
        documents = generer_documents(fiche, scoring)

        # 4. Déposer dans la bibliothèque DD
        deposes = await upload_vers_bibliotheque(input.library_id, documents)

        return {
            "siren": fiche.get("siren"),
            "denomination": fiche.get("denomination") or fiche.get("nom_entreprise"),
            "bibliotheque": input.library_id,
            "scoring_disponible": scoring.get("disponible"),
            "documents_deposes": deposes,
        }
