# Règles de contenu des études de capacité d'investissement

Source : format validé (pilote rang 1, ALAIN THEVENIN SARL) + conventions du projet.

## Ton et principes généraux

- Ton commercial valorisant ; s'adresser au lecteur par « vous » (« votre BFR est parfait »,
  « vous présentez le profil… »). Jamais « [Société] présente le profil… ».
- Document signé **SylvaGreen Conseils** uniquement : jamais de mention du fonds
  (SylvaGreen Capital/Management) hors chapitre 1, jamais de mention de campagne de
  prospection, jamais « Zoho » (dire « CRM de SylvaGreen Conseils »).
- Chapitres numérotés `<h2>`, chacun suivi d'un sous-titre italique `<p class="sous-chap">`
  (sans « — ») et fermé par une note `<p class="sources">Sources : …</p>`.
- Encadrés verdict avec `<div class="encadre">` ; mise en valeur avec `<strong>`.
- Sélection : « parmi 263 distributeurs de granulés de bois en France » (sociétés).

## Variant SOCIÉTÉS — 7 chapitres (4 pages de contenu)

- **Page 1** — Ch. 1 « Objet de l'étude » : encadré « Ce document sert à… », sélection,
  étude gratuite, charte ; puis les trois étages (Conseils / Management, agrément AMF en
  attente / Capital, accord AMF) avec sauts de ligne `p.aise` ; mention « ni offre financière,
  ni conseil en investissement ».
  Ch. 2 « Votre société en un coup d'œil » : table d'identité (dénomination, forme,
  immatriculation, capital, siège, région, SIREN, NAF, effectif, dirigeants avec âges).
- **Page 2** — Ch. 3 « Lecture financière » : tableau 3 exercices (CA, croissance, résultat
  net, EBITDA, marge, BFR, BFR en jours, délais clients/fournisseurs), analyse en « vous »,
  verdict en encadré. Si comptes non publiés : variante « comptes confidentiels ».
- **Page 3** — Ch. 4 « Contexte de la filière » : production 2,117 Mt 2024, −10,4 %,
  prix ~385 €/t T2 2026, positionnement local du site.
  Ch. 5 « Positionnement concurrentiel » : liste distributeurs < 50 km (CRM géolocalisé),
  verdict sur la consolidation du bassin.
- **Page 4** — Ch. 6 « Scieries voisines » : liste scieries < 50 km, mécanismes prise de
  capital / cession-location, cible succession si identifiée. Si aucune occurrence :
  vérifier si la cartographie est incomplète (cas Ferrandi) avant d'écrire « aucune » ;
  sinon angle « créer le point d'ancrage manquant ».
  Ch. 7 « Synthèse » : profil investisseur, accompagnement proposé, encadré « Notre
  engagement », sources ch. 1-6, mention légale finale (`p.mention`).

## Variante PARTICULIERS-TRANSMISSION (validée sur Jean-Louis Duclaux, 12/11/2026)

Pour les particuliers qui veulent **transmettre à leur descendance de leur vivant**.
Gabarit : `template_particulier_transmission.html` (couverture + 6 pages, pagination / 6).
Couverture : colonne droite = personne physique (nom, qualité/ancienne activité, lieu ; **pas
de SIREN ni forme juridique**), rôle « le Destinataire » inchangé. En-têtes `.doc-ref` :
nom de la personne. Ton « vous » identique aux sociétés, mais les chapitres portent sur la
transmission, pas sur la lecture financière de comptes sociaux.

**9 chapitres** : 1. Contexte et profil du donateur (table données/conséquences + encadré
capacité de transmission sans droit) · 2. Produit d'accueil (club deal, alternatives écartées)
· 3. Hypothèses financières (table paramètres + projections) · 4. Les trois scénarios de
donation (A pleine propriété, B nue-propriété avec réserve d'usufruit = recommandé,
C dons de sommes d'argent art. 790 G, chacun avec tables chiffrées) · 5. Comparateur des
scénarios (table) · 6. Plan d'action (étapes numérotées) · 7. Points de vigilance ·
8. Annexe calcul inversé (plafonds exacts à 0 € de droits) + « Lecture pour le club » et
table âge/décote · 9. Sources principales + encadré rappel (donation = acte notarié
irréversible, simulations non contractuelles) + mention légale finale.

**Règles spécifiques validées par Olivier** :
- Jamais « d'autant que l'agrément AMF n'est pas encore obtenu » dans les points de
  vigilance (peu vendeur, mauvaise compréhension du risque). La référence à l'agrément AMF
  reste admise au chapitre 2 pour justifier le club deal.
- La grille fiscale s'adapte aux données du donateur : âge (barème art. 669 CGI : usufruit
  30 % de 71 à 80 ans, 20 % de 81 à 90, 10 % au-delà de 91), nombre d'enfants/petits-enfants,
  abattements 100 000 € / 31 865 € tous les 15 ans, dons de sommes d'argent 790 G avant 80 ans.
  Recalculer TOUS les plafonds (chapitres 1, 5, 8) avec ces données.

## Variante PARTICULIERS simple (brouillon — à valider au premier cas)

- Couverture : colonne droite = personne physique (nom, éventuellement profession,
  adresse, courriel/tél. si connus ; **pas de SIREN**, pas de forme juridique), rôle
  « le Destinataire » inchangé. En-têtes `.doc-ref` : nom de la personne.
- Structure 7 chapitres à adapter : ch. 2 = profil du particulier ; ch. 3 = lecture
  financière personnelle (revenus, patrimoine, capacité d'épargne) au lieu de comptes
  sociaux ; ch. 4 = contexte filière inchangé ; ch. 5/6 = positionnement selon sa
  localisation ; ch. 7 = synthèse.
- La sélection « parmi 263 distributeurs » ne s'applique pas telle quelle : adapter la
  phrase de sélection au profil (ex. « parmi les acteurs de la filière bois-énergie »)
  et le faire valider par Olivier avant publication.
- **Ne rien publier en variante particuliers sans validation explicite d'Olivier** sur
  le canvas et sur cette structure.

## Points de vigilance (erreurs passées)

- Ne pas doubler le préfixe `data:image/png;base64` du logo.
- Âges/dirigeants : jamais « None » ; vérifier chaque valeur issue du CRM.
- Trésorerie fine distinguée de la réserve.
- En-têtes `.page-head` et pieds `.page-foot` : intouchables (hors tokens doc-ref).
- Pas de sous-titre sur la couverture, pas de titre répété en tête de page 1,
  pas de mention « micro-entreprise » sous SYLVAGREEN CONSEILS.
