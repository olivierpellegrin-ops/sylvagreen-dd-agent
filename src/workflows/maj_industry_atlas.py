"""Workflow maj-industry-atlas : pose Industry Distributeur de pellets sur les comptes balises."""

from __future__ import annotations

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .atlas_industry_activities import maj_industry_atlas


class MInput(BaseModel):
    pass


@workflows.workflow.define(
    name="maj-industry-atlas",
    workflow_display_name="MAJ Industry Atlas",
    workflow_description="Pose la valeur Industry distributeur de pellets sur les comptes balises distributeurs.",
)
class MWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: MInput) -> dict:
        return await maj_industry_atlas()
