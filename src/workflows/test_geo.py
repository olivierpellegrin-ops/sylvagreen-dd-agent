"""Workflow test-geo : champs GPS des Leads + comptes geolocalises (scieries, distributeurs)."""

from __future__ import annotations

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai.workflows import workflow

with workflow.unsafe.imports_passed_through():
    from .test_activities import sonder_geo


class TestGeoInput(BaseModel):
    ville: str = "Lecelles"


@workflows.workflow.define(
    name="test-geo",
    workflow_display_name="Test geo Zoho",
    workflow_description="Champs GPS des Leads + scieries/distributeurs geolocalises proches.",
)
class TestGeoWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: TestGeoInput) -> dict:
        return await sonder_geo(ville=input.ville)
