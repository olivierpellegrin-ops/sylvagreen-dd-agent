"""Workflow test-tag : sonde recherche de comptes par balise distributeurs."""

from __future__ import annotations

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .atlas_fix_activities import sonder_tag_distributeurs


class TInput(BaseModel):
    pass


@workflows.workflow.define(
    name="test-tag",
    workflow_display_name="Sonde balise distributeurs",
    workflow_description="Verifie la recherche Accounts par balise distributeurs.",
)
class TWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TInput) -> dict:
        return await sonder_tag_distributeurs()
