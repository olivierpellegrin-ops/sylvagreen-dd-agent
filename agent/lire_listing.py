"""Outil de lecture des listings Excel/CSV pour l'agent investisseurs."""

import csv
import io
import json

import httpx
from openpyxl import load_workbook

from mistralai import agents

MAX_LIGNES = 200


def _lignes_depuis_bytes(data: bytes, nom: str, feuille: str) -> tuple[list[str], list[dict]]:
    nom_l = nom.lower()
    if data[:4] == b"PK\x03\x04" or nom_l.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb[feuille] if feuille and feuille in wb.sheetnames else wb.active
        rows = [["" if c is None else str(c).strip() for c in row] for row in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        texte = data.decode("utf-8-sig", errors="replace")
        rows = [[(c or "").strip() for c in row] for row in csv.reader(io.StringIO(texte))]
    # supprime les lignes entièrement vides
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    header = rows[0]
    lignes = []
    for r in rows[1:]:
        r = (r + [""] * len(header))[: len(header)]
        d = {h: v for h, v in zip(header, r) if h and v}
        if d:
            lignes.append(d)
    return header, lignes


@agents.tool(annotations=agents.ToolAnnotations(read_only_hint=True))
async def lire_listing(source: str, feuille: str = "") -> str:
    """Lit un listing de prospects investisseurs (Excel .xlsx ou CSV, jusqu'à 200 lignes).
    source = URL https publique OU chemin du fichier dans le conteneur (ex. "data/listing.xlsx").
    feuille = nom de l'onglet Excel (optionnel ; premier onglet par défaut).
    Renvoie JSON {source, feuille, colonnes, nb_lignes, lignes}. À appeler en premier pour
    tout listing, puis traiter chaque ligne : verifier_doublons puis creer_prospect."""
    try:
        if source.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(source)
                r.raise_for_status()
                data = r.content
            nom = source.split("/")[-1].split("?")[0] or "listing.xlsx"
        else:
            chemin = source.lstrip("/")
            with open(chemin, "rb") as f:
                data = f.read()
            nom = chemin.split("/")[-1]
        colonnes, lignes = _lignes_depuis_bytes(data, nom, feuille)
        return json.dumps(
            {
                "source": source,
                "feuille": feuille or "première",
                "colonnes": colonnes,
                "nb_lignes": len(lignes),
                "lignes": lignes[:MAX_LIGNES],
                "tronque": len(lignes) > MAX_LIGNES,
            },
            ensure_ascii=False,
        )
    except FileNotFoundError:
        return json.dumps({"erreur": f"Fichier introuvable dans le conteneur : {source}. Demande à Olivier où se trouve le listing."}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"erreur": f"Lecture impossible : {type(e).__name__}: {e}"}, ensure_ascii=False)
