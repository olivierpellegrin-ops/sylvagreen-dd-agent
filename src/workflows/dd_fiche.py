"""Etape 2 : fiche Pappers d'une cible (test API, sans depot de documents)."""

import os

import httpx
from pydantic import BaseModel

from mistralai import workflows

PAPPERS_BASE = "https://api.pappers.fr/v2"


class FicheInput(BaseModel):
    siren: str


@workflows.activity()
async def fetch_fiche(siren: str) -> dict:
    # Authentification Pappers v2 : parametre api_token (ni en-tete api-key, ni api_token).
    params = {"siren": siren, "api_token": os.environ["PAPPERS_API_KEY"]}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{PAPPERS_BASE}/entreprise", params=params)
        r.raise_for_status()
        return r.json()


@workflows.workflow.define(
    name="dd-fiche",
    workflow_display_name="DD Fiche (test Pappers)",
    workflow_description="Recupere la fiche Pappers d'une cible et renvoie une synthese compacte.",
)
class DDFicheWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: FicheInput) -> dict:
        fiche = await fetch_fiche(input.siren)
        finances = fiche.get("finances") or []
        dernier = finances[0] if finances else {}
        return {
            "siren": fiche.get("siren"),
            "denomination": fiche.get("denomination") or fiche.get("nom_entreprise"),
            "forme_juridique": fiche.get("forme_juridique"),
            "etat": fiche.get("etat_entreprise"),
            "nb_exercices": len(finances),
            "dernier_exercice": {
                "annee": dernier.get("annee"),
                "ca": dernier.get("ca"),
                "resultat_net": dernier.get("resultat_net"),
            },
        }
