"""Activites du workflow etude-zoho : HTML -> PDF (WeasyPrint) puis attachement Zoho CRM.

Le PDF (binaire) ne quitte jamais l'activite : les resultats d'activites doivent etre
serialisables en JSON, seule la meta-donnee (pages, taille, ids) est remonte au workflow.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import httpx

import mistralai.workflows as workflows

try:  # lourd : importe au niveau module, le module est charge via imports_passed_through
    from weasyprint import HTML
except Exception:  # pragma: no cover - environnements sans weasyprint
    HTML = None

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"

# Cache du token d'acces Zoho (valide ~1 h) : evite de solliciter accounts.zoho.eu
# a chaque execution (limite "too many requests" quand elles sont lancees en rafale).
_TOKEN_CACHE = {"valeur": None, "expire": 0.0}


def _resoudre_chemin(chemin: str) -> Path:
    """Resout un chemin relatif au repo (le conteneur sert /app comme cwd)."""
    p = Path(chemin)
    if not p.is_absolute():
        for base in (Path.cwd(), Path("/app")):
            cand = base / p
            if cand.exists():
                return cand
    return p


async def _obtenir_html(client: httpx.AsyncClient, chemin: Optional[str], url: Optional[str]) -> bytes:
    if chemin:
        p = _resoudre_chemin(chemin)
        if not p.exists():
            raise FileNotFoundError(f"Etude introuvable dans le conteneur : {chemin}")
        return p.read_bytes()
    if url:
        r = await client.get(url)
        r.raise_for_status()
        return r.content
    raise ValueError("Fournir 'chemin' (fichier du repo) ou 'url' (URL signee).")


def _vers_pdf(html: bytes) -> tuple:
    if HTML is None:
        raise RuntimeError("weasyprint n'est pas installe dans ce conteneur")
    doc = HTML(string=html.decode("utf-8"), encoding="utf-8").render()
    return doc.write_pdf(), len(doc.pages)


async def _refresh_access_token(client: httpx.AsyncClient) -> str:
    now = time.time()
    if _TOKEN_CACHE["valeur"] and now < _TOKEN_CACHE["expire"]:
        return _TOKEN_CACHE["valeur"]
    params = {
        "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN", ""),
        "client_id": os.environ.get("ZOHO_CLIENT_ID", ""),
        "client_secret": os.environ.get("ZOHO_CLIENT_SECRET", ""),
        "grant_type": "refresh_token",
    }
    t = await client.post(ZOHO_ACCOUNTS, params=params)
    body = t.json()
    if "access_token" not in body:
        raise ValueError(f"Refresh token Zoho refuse : HTTP {t.status_code} / {body}")
    _TOKEN_CACHE["valeur"] = body["access_token"]
    _TOKEN_CACHE["expire"] = time.time() + 3300  # marge de 5 min sur les ~60 min de validite
    return body["access_token"]


async def _chercher_fiche(client: httpx.AsyncClient, headers: dict, siren: str, module: Optional[str]) -> dict:
    essais = (
        [(module, "SIREN" if module == "Leads" else "SIRET")]
        if module
        else [("Accounts", "SIRET"), ("Leads", "SIREN")]
    )
    for mod, champ in essais:
        r = await client.get(
            f"{ZOHO_CRM}/{mod}/search",
            params={"criteria": f"({champ}:equals:{siren})", "per_page": 5},
            headers=headers,
        )
        if r.status_code != 200:
            continue
        data = r.json().get("data", [])
        if data:
            rec = data[0]
            nom = (
                rec.get("Account_Name")
                or rec.get("Full_Name")
                or rec.get("Company")
                or rec.get("Last_Name")
            )
            return {
                "module": mod,
                "id": rec["id"],
                "nom": nom,
                "ambigu": [x["id"] for x in data] if len(data) > 1 else None,
            }
    raise ValueError(f"Aucune fiche trouvee pour SIREN {siren} dans {[m for m, _ in essais]}")


async def _attacher_pdf(client: httpx.AsyncClient, headers: dict, module: str, record_id: str, pdf: bytes, nom_pdf: str) -> str:
    # remplace une piece jointe homonyme (rejouable sans doublon)
    r = await client.get(f"{ZOHO_CRM}/{module}/{record_id}/Attachments", headers=headers)
    if r.status_code == 200:
        for att in r.json().get("data", []):
            if att.get("File_Name") == nom_pdf:
                await client.delete(
                    f"{ZOHO_CRM}/{module}/{record_id}/Attachments/{att['id']}",
                    headers=headers,
                )
    r = await client.post(
        f"{ZOHO_CRM}/{module}/{record_id}/Attachments",
        headers=headers,
        files={"file": (nom_pdf, pdf, "application/pdf")},
    )
    body = None
    try:
        body = r.json()
    except Exception:
        pass
    if r.status_code not in (200, 201) or not (body or {}).get("data"):
        raise ValueError(f"Attachement refuse : HTTP {r.status_code} / {str(body or r.text)[:400]}")
    return body["data"][0]["details"]["id"]


async def _ajouter_note(client: httpx.AsyncClient, headers: dict, module: str, record_id: str, note: str) -> dict:
    r = await client.get(
        f"{ZOHO_CRM}/{module}/{record_id}",
        params={"fields": "id,Description"},
        headers=headers,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    desc = (data[0].get("Description") or "") if data else ""
    if note in desc:
        return {"deja_present": True}
    nouvelle = (desc + ("
" if desc else "") + note).strip()
    r2 = await client.put(
        f"{ZOHO_CRM}/{module}/{record_id}",
        headers=headers,
        json={"data": [{"id": record_id, "Description": nouvelle}]},
    )
    r2.raise_for_status()
    return {"deja_present": False, "description_maj": True}


@workflows.activity()
async def traiter_etude(
    siren: str,
    chemin: Optional[str] = None,
    url: Optional[str] = None,
    nom_pdf: Optional[str] = None,
    module: Optional[str] = None,
    ajouter_note: bool = True,
    texte_note: Optional[str] = None,
) -> dict:
    """Charge l'etude HTML, la convertit en PDF A4 et l'attache a la fiche prospect Zoho."""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        html = await _obtenir_html(client, chemin, url)
        source_nom = (chemin or url or "").split("/")[-1].replace(".html", "")
        nom = nom_pdf or (source_nom + ".pdf")
        pdf, pages = _vers_pdf(html)
        token = await _refresh_access_token(client)
        headers = {"Authorization": "Zoho-oauthtoken " + token}
        fiche = await _chercher_fiche(client, headers, siren, module)
        attachment_id = await _attacher_pdf(client, headers, fiche["module"], fiche["id"], pdf, nom)
        note_statut = None
        if ajouter_note:
            note = texte_note or (
                "Etude de capacite d'investissement SylvaGreen Conseils (PDF joint) - 2026-11-12."
            )
            note_statut = await _ajouter_note(client, headers, fiche["module"], fiche["id"], note)
        return {
            "fiche": fiche,
            "pdf": {"nom": nom, "pages": pages, "taille_octets": len(pdf)},
            "attachment_id": attachment_id,
            "note": note_statut,
        }
