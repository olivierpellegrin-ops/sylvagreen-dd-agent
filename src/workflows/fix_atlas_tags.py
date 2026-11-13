"""Workflow fix-atlas-tags : pose les balises distributeurs + Atlas 2019."""

from __future__ import annotations

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .atlas_fix_activities import baliser_atlas


class FixInput(BaseModel):
    pass


@workflows.workflow.define(
    name="fix-atlas-tags",
    workflow_display_name="Fix balises Atlas",
    workflow_description="Pose les balises distributeurs + Atlas 2019 sur les comptes de l'atlas.",
)
class FixAtlasWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: FixInput) -> dict:
        return await baliser_atlas()
