"""Workflow dd-fiche : synthese Pappers d'une cible (etape de test)."""

from __future__ import annotations

import mistralai.workflows as workflows
from mistralai.workflows import workflow
from pydantic import BaseModel

# Les activites (qui importent httpx) doivent passer par imports_passed_through,
# sinon le sandbox Temporal tente de re-importer httpx et echoue.
with workflow.unsafe.imports_passed_through():
    from .dd_activities import fetch_fiche


class FicheInput(BaseModel):
    siren: str


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
