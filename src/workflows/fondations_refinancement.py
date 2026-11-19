"""Workflow fondations-refinancement : crée les champs personnalisés du module
Devis (Quotes) du process « devis cession/location & location/cession ».

- Idempotent : compare les libellés existants (normalisés accents/casse/espaces)
  et ne crée que les champs absents.
- dry_run=True : liste le plan sans rien écrire.
- Sortie compacte : jamais le méta complet du module.
- La sonde initiale lit un enregistrement pour vérifier le module : le token
  actuel n'a PAS le scope modules.quotes (refus 401), ce qui n'empêche pas la
  création des champs (scope settings.fields). Le refus est donc signalé sans
  interrompre le workflow ; il faudra ajouter le scope au prochain Generate Code
  pour lire/écrire les devis depuis l'agent.
- Chaque champ tente plusieurs formes de payload (l'API Zoho évolue entre
  pick_list_values v1/v2, wrapper « field » ou non) et consigne l'erreur
  exacte en cas d'échec, champ par champ.
"""

from __future__ import annotations

import os
import unicodedata

import mistralai.workflows as workflows
from mistralai.workflows import workflow
from pydantic import BaseModel

with workflow.unsafe.imports_passed_through():
    import httpx

ZOHO_ACCOUNTS = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_CRM = "https://www.zohoapis.eu/crm/v8"
MODULE = "Quotes"

# Champs du § « Traçabilité Zoho CRM » du process (libellés du document de référence).
CHAMPS = [
    {"label": "Type de produit", "type": "picklist",
     "valeurs": ["Cession/Location", "Location/Cession"]},
    {"label": "Banque partenaire", "type": "lookup"},
    {"label": "Taux nominal", "type": "double"},
    {"label": "Taux d'indexation", "type": "double"},
    {"label": "Date de validité du taux", "type": "date"},
    {"label": "Montant équipement", "type": "currency"},
    {"label": "Apport / subventions (CEE)", "type": "currency"},
    {"label": "Durée des loyers", "type": "picklist",
     "valeurs": ["36 mois", "48 mois", "60 mois", "72 mois", "84 mois"]},
    {"label": "Valeur résiduelle / option d'achat", "type": "currency"},
    {"label": "Loyer périodique", "type": "currency"},
    {"label": "Coût total financement", "type": "currency"},
    {"label": "Marge / commission SylvaGreen", "type": "currency"},
    {"label": "Distributeur apporteur", "type": "lookup"},
    {"label": "Source de la demande", "type": "picklist",
     "valeurs": ["Client final", "Distributeur partenaire", "Site web",
                 "Recommandation"]},
    {"label": "Version du devis", "type": "text", "longueur": 30},
    {"label": "Motif de re-chiffrage", "type": "text", "longueur": 200},
    {"label": "Statut devis étendu", "type": "picklist",
     "valeurs": ["Reçu", "Qualifié", "Simulé", "À valider", "Envoyé",
                 "Expiré", "Re-chiffré", "Accepté", "Perdu"]},
]


def _norm(label: str) -> str:
    """Clé de comparaison : minuscules, sans accents, sans espaces."""
    sans_accents = unicodedata.normalize("NFKD", label or "")
    ascii_ = "".join(c for c in sans_accents if not unicodedata.combining(c))
    return "".join(ascii_.lower().split())


def _payloads(spec: dict) -> list:
    """Tuples (nom_de_tentative, body) pour un champ, du plus probable au repli."""
    label = spec["label"]
    t = spec["type"]
    if t == "picklist":
        v2 = {"field_label": label, "data_type": "picklist",
              "pick_list_values_v2": [
                  {"display_value": v, "sequence_number": i + 1}
                  for i, v in enumerate(spec["valeurs"])]}
        v1 = {"field_label": label, "data_type": "picklist",
              "pick_list_values": [{"display_value": v} for v in spec["valeurs"]]}
        corps = [("picklist_v2", v2), ("picklist_v1", v1)]
    elif t == "lookup":
        lk = {"field_label": label, "data_type": "lookup",
              "lookup": {"related_module": {"api_name": "Accounts"}}}
        lk2 = {"field_label": label, "data_type": "lookup",
               "lookup": {"related_module": "Accounts"}}
        tx = {"field_label": label, "data_type": "text", "length": 120}
        corps = [("lookup_objet", lk), ("lookup_chaine", lk2), ("repli_texte", tx)]
    elif t == "double":
        d = {"field_label": label, "data_type": "double"}
        p = {"field_label": label, "data_type": "percent"}
        corps = [("double", d), ("repli_percent", p)]
    elif t == "currency":
        corps = [("currency", {"field_label": label, "data_type": "currency"})]
    elif t == "date":
        corps = [("date", {"field_label": label, "data_type": "date"})]
    else:  # texte
        corps = [("texte", {"field_label": label, "data_type": "text",
                           "length": spec.get("longueur", 120)})]
    tentatives = []
    for nom, body in corps:
        tentatives.append((nom + "_enveloppe", {"field": body}))
        tentatives.append((nom + "_nu", body))
    return tentatives


def _erreur_courte(resp) -> str:
    try:
        corps = resp.json()
        for m in corps.get("data") or []:
            if isinstance(m, dict) and m.get("message"):
                return f"HTTP {resp.status_code} {m.get('code')} : {str(m['message'])[:200]}"
        return f"HTTP {resp.status_code} : {str(corps)[:200]}"
    except Exception:
        return f"HTTP {resp.status_code} : {resp.text[:200]}"


@workflows.activity()
async def fondations(dry_run: bool) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        params = {
            "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN", ""),
            "client_id": os.environ.get("ZOHO_CLIENT_ID", ""),
            "client_secret": os.environ.get("ZOHO_CLIENT_SECRET", ""),
            "grant_type": "refresh_token",
        }
        t = await client.post(ZOHO_ACCOUNTS, params=params)
        body = t.json()
        if "access_token" not in body:
            raise ValueError(
                f"Refresh token Zoho refuse : HTTP {t.status_code} / {body}"
            )
        headers = {"Authorization": "Zoho-oauthtoken " + body["access_token"]}

        # 0) Sonde : les ENREGISTREMENTS du module exigent le scope
        # modules.quotes (absent du token actuel). Un refus 401 est signalé
        # sans interrompre : la création des champs passe par settings.fields.
        sonde = await client.get(
            f"{ZOHO_CRM}/{MODULE}",
            params={"per_page": 2, "page": 1, "fields": "id"},
            headers=headers,
        )
        module_info = {"http": sonde.status_code}
        if sonde.status_code == 200:
            data = sonde.json().get("data", [])
            module_info["nb_page"] = len(data)
            module_info["exemple_id"] = [r.get("id") for r in data]
        elif sonde.status_code == 401:
            module_info["acces_enregistrements"] = (
                "refuse : ajouter ZohoCRM.modules.quotes.ALL au prochain "
                "Generate Code. Détail : " + _erreur_courte(sonde)
            )

        # 1) Champs existants du module
        rf = await client.get(
            f"{ZOHO_CRM}/settings/fields", params={"module": MODULE}, headers=headers
        )
        if rf.status_code != 200:
            return {"module": MODULE, "settings_inaccessible": _erreur_courte(rf),
                    "dry_run": dry_run}
        existants = {}
        for f in rf.json().get("fields", []):
            existants[_norm(f.get("field_label"))] = {
                "api_name": f.get("api_name"),
                "data_type": f.get("data_type"),
                "label": f.get("field_label"),
            }

        # 2) Création des champs manquants
        resultat = []
        for spec in CHAMPS:
            cle = _norm(spec["label"])
            item = {"label": spec["label"], "type": spec["type"]}
            if cle in existants:
                item["statut"] = "existe"
                item["api_name"] = existants[cle]["api_name"]
                item["data_type_reel"] = existants[cle]["data_type"]
                resultat.append(item)
                continue
            if dry_run:
                item["statut"] = "a_creer"
                resultat.append(item)
                continue
            cree = None
            erreurs = []
            for nom, payload in _payloads(spec):
                r = await client.post(
                    f"{ZOHO_CRM}/settings/fields",
                    params={"module": MODULE}, headers=headers, json=payload,
                )
                if r.status_code in (200, 201):
                    donnees = r.json().get("data") or [{}]
                    fld = donnees[0] if isinstance(donnees[0], dict) else {}
                    cree = {"tentative": nom, "api_name": fld.get("api_name"),
                            "id": fld.get("id"), "label": fld.get("field_label")}
                    break
                erreurs.append(f"{nom} -> {_erreur_courte(r)}")
            if cree:
                item["statut"] = "cree"
                item["tentative"] = cree["tentative"]
                item["api_name"] = cree["api_name"]
                item["id"] = cree["id"]
            else:
                item["statut"] = "erreur"
                item["erreurs"] = erreurs[:4]
            resultat.append(item)

        return {
            "module": MODULE,
            "module_info": module_info,
            "dry_run": dry_run,
            "nb_cible": len(CHAMPS),
            "champs": resultat,
        }


class FondationsRefinancementInput(BaseModel):
    """dry_run=True : plan des créations sans écriture. False : création effective."""

    dry_run: bool = True


@workflows.workflow.define(
    name="fondations-refinancement",
    workflow_display_name="Fondations Refinancement",
    workflow_description=(
        "Crée (idempotent) les champs personnalisés du module Devis (Quotes) "
        "du process devis cession/location & location/cession : produit, taux, "
        "montants, marge, distributeur apporteur, statut devis étendu..."
    ),
)
class FondationsRefinancementWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: FondationsRefinancementInput) -> dict:
        return await fondations(input.dry_run)
