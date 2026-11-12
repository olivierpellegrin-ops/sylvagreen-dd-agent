"""Workflow etude-zoho : convertit une etude HTML en PDF et l'attache a la fiche prospect Zoho CRM.

Entrees : SIREN de la societe + source du HTML (fichier du repo agent/data/etudes/...
ou URL signee). Le PDF remplace une piece jointe homonyme (rejouable sans doublon).
Tout le traitement binaire reste dans une seule activite.
"""

from __future__ import annotations

from typing import Optional

import mistralai.workflows as workflows
from mistralai.workflows import workflow
from pydantic import BaseModel

with workflow.unsafe.imports_passed_through():
    from .etude_activities import traiter_etude


class EtudeZohoInput(BaseModel):
    """SIREN cible + source HTML ; le PDF est nomme d'apres l'etude si absent."""

    siren: str
    chemin: Optional[str] = None
    url: Optional[str] = None
    nom_pdf: Optional[str] = None
    module: Optional[str] = None
    ajouter_note: bool = True
    texte_note: Optional[str] = None


@workflows.workflow.define(
    name="etude-zoho",
    workflow_display_name="Etude vers Zoho",
    workflow_description=(
        "Convertit une etude de capacite HTML en PDF (A4, marges 0 du @page) "
        "et l'attache a la fiche prospect Zoho CRM correspondant au SIREN."
    ),
)
class EtudeZohoWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: EtudeZohoInput) -> dict:
        return await traiter_etude(
            siren=input.siren,
            chemin=input.chemin,
            url=input.url,
            nom_pdf=input.nom_pdf,
            module=input.module,
            ajouter_note=input.ajouter_note,
            texte_note=input.texte_note,
        )
