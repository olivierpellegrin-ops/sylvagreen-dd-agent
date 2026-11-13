"""Workflow import-atlas : cree les comptes distributeurs de l'atlas (sans contacts)."""

from __future__ import annotations

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .atlas_activities import importer_atlas


class AtlasInput(BaseModel):
    dry_run: bool = False


@workflows.workflow.define(
    name="import-atlas",
    workflow_display_name="Import Atlas distributeurs",
    workflow_description="Cree les comptes Atlas (Industry Distributeurs pellets, balises distributeurs + Atlas 2019), sans contacts.",
)
class ImportAtlasWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: AtlasInput) -> dict:
        return await importer_atlas(dry_run=input.dry_run)
