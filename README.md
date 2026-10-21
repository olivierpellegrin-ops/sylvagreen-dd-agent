# DD Ingest — agent d'alimentation des bibliothèques DD (SylvaGreen Capital)

Workflow Mistral qui collecte les données publiques d'une société cible via
l'API Pappers et alimente automatiquement la bibliothèque « DD + cible »
(aujourd'hui : BB BGI, Bourgogne Granulés Industrie).

## Pipeline

1. resoudre_siren (si seul le nom est fourni)
2. fetch_fiche (API Pappers v2, fiche complète)
3. generer_documents (TRV-01, FIN-01, JUR-01, ANX-00)
4. upload_vers_bibliotheque (API Mistral Libraries)

## Secrets (Secrets Manager du workspace, jamais dans le repo)

- PAPPERS_API_KEY : clé API du compte Pappers (onglet API de pappers.fr)
- MISTRAL_API_KEY : clé API Mistral (accès Libraries)

## Exécution

Entrée : "siren" + "library_id", ou "nom_entreprise" seul.

Exemple d'entrée :

    {"nom_entreprise": "Bourgogne Granulés Industrie"}

Le workflow renvoie le SIREN, la dénomination et la liste des documents déposés.
