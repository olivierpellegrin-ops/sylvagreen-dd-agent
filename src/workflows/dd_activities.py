"""Activites DD — I/O isolees hors du sandbox du workflow."""

import os

import httpx

import mistralai.workflows as workflows


PAPPERS_BASE = "https://api.pappers.fr/v2"


@workflows.activity()
async def fetch_fiche(siren: str) -> dict:
    """Recupere la fiche Pappers de la cible (auth : parametre api_token)."""
    params = {"siren": siren, "api_token": os.environ["PAPPERS_API_KEY"]}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{PAPPERS_BASE}/entreprise", params=params)
        r.raise_for_status()
        return r.json()
