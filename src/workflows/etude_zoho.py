"""Workflow etude-zoho : convertit une etude HTML en PDF et l'attache a la fiche prospect Zoho CRM.

Entrees : SIREN de la societe + source du HTML (fichier du repo agent/data/etudes/...
ou URL signee). Le PDF remplace une piece jointe homonyme (rejouable sans doublon).
"""

from __future__ import annotations

from typing import Optional

import mistralai.workflows as workflows
from mistralai.workflows import workflow
from pydantic import BaseModel

with workflow.unsafe.imports_passed_through():
    from .etude_activities import (
        ajouter_note,
        attacher_pdf,
        chercher_fiche,
        html_vers_pdf,
        obtenir_html,
        refresh_access_token,
    )


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
        source = await obtenir_html(chemin=input.chemin, url=input.url)
        base = source["source"].split("/")[-1].replace(".html", "")
        nom_pdf = input.nom_pdf or (base + ".pdf")
        conv = await html_vers_pdf(source["html"], nom_pdf)
        token = await refresh_access_token()
        fiche = await chercher_fiche(token, input.siren, module=input.module)
        att = await attacher_pdf(token, fiche["module"], fiche["id"], conv["pdf"], conv["nom_pdf"])
        note_statut = None
        if input.ajouter_note:
            note = input.texte_note or (
                "Etude de capacite d'investissement SylvaGreen Conseils (PDF joint) - 2026-11-12."
            )
            note_statut = await ajouter_note(token, fiche["module"], fiche["id"], note)
        return {
            "fiche": fiche,
            "pdf": {"nom": conv["nom_pdf"], "pages": conv["pages"], "taille_octets": conv["taille_octets"]},
            "attachment_id": att["attachment_id"],
            "note": note_statut,
        }
