"""Activites du workflow etude-zoho : HTML -> PDF (WeasyPrint) puis attachement Zoho CRM."""

from __future__ import annotations

import os
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


def _resoudre_chemin(chemin: str) -> Path:
    """Resout un chemin relatif au repo (le conteneur sert /app comme cwd)."""
    p = Path(chemin)
    if not p.is_absolute():
        for base in (Path.cwd(), Path("/app")):
            cand = base / p
            if cand.exists():
                return cand
    return p


@workflows.activity()
async def obtenir_html(chemin: Optional[str] = None, url: Optional[str] = None) -> dict:
    """Charge le HTML de l'etude depuis un fichier du repo ou une URL signee."""
    if chemin:
        p = _resoudre_chemin(chemin)
        if not p.exists():
            raise FileNotFoundError(f"Etude introuvable dans le conteneur : {chemin}")
        data = p.read_bytes()
        return {"html": data, "source": str(p)}
    if url:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            return {"html": r.content, "source": url}
    raise ValueError("Fournir 'chemin' (fichier du repo) ou 'url' (URL signee).")


@workflows.activity()
async def html_vers_pdf(html: bytes, nom_pdf: str) -> dict:
    """Convertit le HTML en PDF A4 (marges 0 gerees par le @page du document)."""
    if HTML is None:
        raise RuntimeError("weasyprint n'est pas installe dans ce conteneur")
    doc = HTML(string=html.decode("utf-8"), encoding="utf-8").render()
    pdf = doc.write_pdf()
    return {"pdf": pdf, "nom_pdf": nom_pdf, "pages": len(doc.pages), "taille_octets": len(pdf)}


@workflows.activity()
async def refresh_access_token() -> str:
    """Echange le refresh token (secrets d'environnement) contre un access token."""
    params = {
        "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN", ""),
        "client_id": os.environ.get("ZOHO_CLIENT_ID", ""),
        "client_secret": os.environ.get("ZOHO_CLIENT_SECRET", ""),
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        t = await client.post(ZOHO_ACCOUNTS, params=params)
        body = t.json()
        if "access_token" not in body:
            raise ValueError(f"Refresh token Zoho refuse : HTTP {t.status_code} / {body}")
        return body["access_token"]


@workflows.activity()
async def chercher_fiche(access_token: str, siren: str, module: Optional[str] = None) -> dict:
    """Cherche la fiche prospect par SIREN : Accounts (champ SIRET) puis Leads (champ SIREN)."""
    headers = {"Authorization": "Zoho-oauthtoken " + access_token}
    if module:
        essais = [(module, "SIREN" if module == "Leads" else "SIRET")]
    else:
        essais = [("Accounts", "SIRET"), ("Leads", "SIREN")]
    async with httpx.AsyncClient(timeout=60) as client:
        for mod, champ in essais:
            r = await client.get(
                f"{ZOHO_CRM}/{mod}/search",
                params={"criteria": f"({champ}:equals:{siren})", "per_page": 5},
                headers=headers,
            )
            if r.status_code == 204:
                continue
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
                if len(data) > 1:
                    return {"module": mod, "id": rec["id"], "nom": nom, "ambigu": [x["id"] for x in data]}
                return {"module": mod, "id": rec["id"], "nom": nom}
    raise ValueError(f"Aucune fiche trouvee pour SIREN {siren} dans {[m for m, _ in essais]}")


@workflows.activity()
async def attacher_pdf(
    access_token: str, module: str, record_id: str, pdf: bytes, nom_pdf: str, remplacer: bool = True
) -> dict:
    """Attache le PDF a la fiche ; remplace une piece jointe homonyme si demande."""
    headers = {"Authorization": "Zoho-oauthtoken " + access_token}
    async with httpx.AsyncClient(timeout=120) as client:
        if remplacer:
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
        return {"attachment_id": body["data"][0]["details"]["id"], "nom_pdf": nom_pdf}


@workflows.activity()
async def ajouter_note(access_token: str, module: str, record_id: str, note: str) -> dict:
    """Ajoute une ligne dans la Description de la fiche (journal, jamais de donnees structurees)."""
    headers = {"Authorization": "Zoho-oauthtoken " + access_token}
    async with httpx.AsyncClient(timeout=60) as client:
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
        nouvelle = (desc + ("\n" if desc else "") + note).strip()
        r2 = await client.put(
            f"{ZOHO_CRM}/{module}/{record_id}",
            headers=headers,
            json={"data": [{"id": record_id, "Description": nouvelle}]},
        )
        r2.raise_for_status()
        return {"deja_present": False, "description_maj": True}
