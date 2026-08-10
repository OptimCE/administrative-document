<p align="center">
  <img src="logo.svg" alt="Logo OptimCE" width="160">
</p>

# OptimCE — Documents administratifs

[![Site web](https://img.shields.io/badge/Site%20web-optimce.be-2e7d32.svg)](https://www.optimce.be)
[![Licence](https://img.shields.io/badge/Licence-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-43a047.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](README.nl.md)

**Documents administratifs** est le microservice des dossiers réglementaires de
la plateforme OptimCE. Il constitue la **source de vérité administrative** des
documents qu'une communauté d'énergie doit produire, transmettre et suivre tout
au long de son cycle de vie : notification de création, rapportage annuel,
autorisations de partage, modifications.

Il répond aux questions auxquelles un processus papier ne répond pas : *qu'a-t-on
exactement transmis, quand, sur la base de quelles données, et quelle est la
prochaine échéance ?* Chaque changement de statut est une entrée de journal
immuable, chaque fichier stocké est une version immuable, et les échéances
réglementaires sont calculées automatiquement à partir de ces transitions plutôt
que suivies dans un tableur.

OptimCE est une plateforme open source de gestion des communautés d'énergie
renouvelable, conçue pour le contexte belge du partage d'énergie. Pour en savoir
plus sur le projet, consultez [www.optimce.be](https://www.optimce.be). Ce
service est normalement exécuté au sein de la plateforme complète : voir le
[monorepo de développement](https://github.com/OptimCE/monorepo), qui agrège
tous les services OptimCE et fournit l'environnement Docker Compose pour les
exécuter ensemble.

## Fonctionnement

Le service fournit deux déployables issus d'une même base de code — une API
[FastAPI](https://fastapi.tiangolo.com/) (`main:app`) et un worker NATS
(`worker.main`) qui exécute les consommateurs de génération et de résultats ainsi
que le planificateur d'échéances — construits autour de quatre idées :

- **Une machine à états journalisée.** Les documents suivent le cycle
  `brouillon → prêt → transmis → accusé de réception` (plus `obsolète`) ; les
  dossiers suivent `en préparation → soumis → complet → clôturé` (plus `caduc`).
  Un statut n'est jamais modifié sur place : chaque changement ajoute un
  `status_event` immuable (auteur, horodatage, statuts source et cible,
  contexte), et la colonne de statut n'est qu'un cache que la base de données
  valide par rapport à ce journal. Corriger une erreur consiste à enregistrer une
  nouvelle transition corrective tracée — l'historique n'est jamais réécrit.
- **Des échéances calculées.** Les délais réglementaires sont de la
  configuration, pas du code. Une ligne `deadline_rule` exprime « lorsque *ceci*
  se produit, une échéance *de ce type* tombe N jours ouvrés ou N mois plus
  tard » ; une transition évalue les règles correspondantes et matérialise les
  échéances. Le calcul en jours ouvrés respecte les jours fériés belges, fêtes
  mobiles comprises.
- **Des versions immuables.** Chaque fichier téléversé devient une version
  numérotée, adressée par son empreinte. Un nouveau téléversement crée une
  nouvelle version ; une version déjà transmise ne peut plus être modifiée, ce
  qui garantit de toujours pouvoir récupérer exactement ce qui a été envoyé.
- **Des dépôts générés.** Les formulaires obligatoires sont produits à partir des
  données de la communauté plutôt que remplis à la main : une étape de
  préremplissage construit la charge utile depuis le CRM, la génération fige
  celle-ci en instantané et effectue le rendu du modèle enregistré via le service
  de génération de documents de la plateforme, et le résultat arrive comme une
  nouvelle version immuable. L'instantané est capturé au moment de la demande, ce
  qui rend toujours reproductible ce qui a été déposé. Un seul rendu peut être en
  cours par document.

Deux bases **PostgreSQL** sont utilisées : la base du CRM (source en lecture
seule pour les communautés, membres, compteurs et abonnements) et une base locale
qui détient les dossiers, documents, versions, le journal de statuts et les
échéances. Les fichiers stockés résident dans un stockage objet **compatible S3**
(MinIO en développement). Les événements de statut et d'échéance sont publiés sur
**NATS JetStream** pour les autres services. Traces, métriques et journaux sont
émis via **OpenTelemetry**.

## Structure du dépôt

| Chemin | Description |
|---|---|
| `api/` | Couche HTTP — routes de santé et des documents administratifs, schémas, service et repository |
| `domain/` | Logique métier pure — machine à états, moteur de règles d'échéance et calendrier ouvré belge |
| `ports/` | Adaptateurs vers l'extérieur — lecture seule du CRM, publication d'événements NATS et port de rendu vers la génération de documents |
| `worker/` | Consommateurs NATS des demandes de génération et de leurs résultats, plus le planificateur quotidien d'échéances |
| `document-templates/` | Les modèles enregistrés — les formulaires propres au régulateur et leurs manifestes |
| `shared/` | Constantes, catalogue d'erreurs, modèles ORM, utilitaires |
| `core/` | Infrastructure transverse — configuration, base de données, file, stockage, sécurité, middlewares, i18n, tracing, métriques, journalisation |
| `tests/` | Suite de tests (pytest) |
| `locales/` | Messages d'erreur de l'API traduits (en, fr, nl, de) |
| `scripts/` | Utilitaires — export OpenAPI, schéma SQL, migrations et données de référence |

## API

Tous les endpoints requièrent une authentification et un abonnement actif de la
communauté (voir [Authentification](#authentification)). La passerelle ajoute le
préfixe externe `/administrative-document` ; les chemins ci-dessous sont donc
relatifs.

L'accès est par défaut réservé aux gestionnaires : chaque route exige au moins le
rôle gestionnaire, les écritures dans les registres de modèles et de règles
d'échéance exigent le rôle administrateur, et `GET /filings/mine` est l'unique
endpoint ouvert aux membres — il ne porte aucune restriction de rôle parce que le
service réduit chaque dossier aux seules lignes de l'appelant avant de construire
la réponse.

| Méthode | Chemin | Rôle |
|---|---|---|
| `GET` / `POST` | `/dossiers` | Lister (paginé, filtrable) ou ouvrir un dossier |
| `GET` / `PATCH` | `/dossiers/{id}` | Consulter un dossier avec ses documents et échéances, ou modifier sa référence / ses métadonnées |
| `POST` | `/dossiers/{id}/transition` · `rollback` | Enregistrer un changement de statut du dossier, ou une correction |
| `GET` | `/dossiers/{id}/timeline` | Le journal immuable du dossier et de ses documents |
| `GET` | `/dossiers/{id}/deadlines` | Les échéances calculées pour un dossier |
| `GET` / `POST` | `/dossiers/{id}/documents` | Lister ou ajouter un document au dossier |
| `GET` | `/documents/{id}` | Consulter un document et ses versions |
| `POST` | `/documents/{id}/versions` | Téléverser une nouvelle version immuable (`multipart/form-data`) |
| `GET` | `/documents/{id}/versions/{versionId}/file` | Télécharger une version stockée |
| `POST` | `/documents/{id}/transition` | Enregistrer un changement de statut du document |
| `POST` | `/documents/{id}/mark-ready` · `mark-sent` · `acknowledge` · `rollback` | Raccourcis pour les transitions courantes |
| `GET` | `/documents/{id}/prefill` | Construire la charge utile depuis le CRM, sans rien persister |
| `POST` | `/documents/{id}/generate` | Figer l'instantané et demander le rendu (`409` si un rendu est déjà en cours) |
| `GET` | `/documents/{id}/render-status` | Interroger le rendu en cours |
| `GET` | `/deadlines` | Tableau de bord des échéances, tous dossiers confondus |
| `POST` | `/deadlines/{id}` | Marquer une échéance respectée ou annulée |
| `GET` | `/filings/mine` | Les dépôts propres à l'appelant, réduits à ses lignes avant construction de la réponse |
| `GET` | `/sharing-operations` | Les opérations de partage du CRM auxquelles rattacher un dossier |
| `GET` / `POST` | `/templates`, `/deadline-rules` | Consulter les registres (surcharges propres et valeurs par défaut) ou ajouter une surcharge |
| `PATCH` | `/templates/{id}`, `/deadline-rules/{id}` | Modifier la surcharge propre à la communauté |
| `POST` | `/maintenance/deadline-sweep` | Déclencher le balayage des échéances hors planification |

Les endpoints de santé sont servis sous `/health` (`/health/liveness`,
`/health/readiness`, `/health/health`). La documentation OpenAPI interactive
(`/docs`, `/redoc`, `/openapi.json`) n'est activée que si `ENV=local`.

### Authentification

Le service n'effectue aucune connexion propre. Dans la plateforme OptimCE, une
passerelle [KrakenD](https://www.krakend.io/) et
[Keycloak](https://www.keycloak.org/) authentifient la requête et injectent les
en-têtes d'identité (`x-user-id`, `x-community-id`, `x-user-groups`,
`x-user-orgs`). Le rôle de l'appelant est résolu pour la communauté active, et
l'accès à la fonctionnalité est conditionné à un abonnement actif.

## Démarrage

### Prérequis

- Docker et Docker Compose (recommandé), **ou** Python 3.12 pour un
  développement local autonome

### Via la stack OptimCE (recommandé)

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

Le service tourne sous le nom `administrative-document` :

```bash
curl http://localhost:8006/health/readiness
```

### En autonome

```bash
git clone https://github.com/OptimCE/administrative-document.git
cd administrative-document
python -m venv .venv
# Windows : .venv\Scripts\activate  |  Unix : source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env
```

Appliquez le schéma et les données de référence, puis démarrez l'API (NATS, MinIO
et PostgreSQL doivent rester accessibles — la stack du monorepo est le moyen le
plus simple de les fournir) :

```bash
psql "$LOCAL_DATABASE_URL" -f scripts/sql/schema.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0001_wal_deadline_rules.sql
uvicorn main:app --reload
```

## Configuration

La configuration est lue depuis l'environnement ; `.env.exemple` documente chaque
variable. Les principaux groupes sont :

- **Base CRM** (`CRM_DATABASE_URL`, réglages de pool `CRM_DB_*`)
- **Base locale** (`LOCAL_DATABASE_URL`, réglages de pool `LOCAL_DB_*`)
- **Messagerie** (`NATS_URL`)
- **Stockage objet** (`STORAGE_ENDPOINT`, `STORAGE_BUCKET`,
  `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, `STORAGE_REGION`, `OUTPUT_BUCKET`)
- **CORS** (`ALLOW_ORIGIN`)
- **Observabilité** (`LOGGING_TOKEN`, `LOGGING_TRACES_URL`, `LOGGING_LOGS_URL`,
  `LOGGING_METRICS_URL`)
- **Sélecteur d'environnement** (`ENV` : `local`, `test`, `staging`,
  `production`)

## Schéma de base de données

Il n'y a pas d'outil de migration. `scripts/sql/schema.sql` est l'unique source
de vérité pour la base locale et est reflété par `shared/models/local_models.py` ;
les évolutions sont ajoutées sous forme de fichiers non réversibles dans
`scripts/sql/migrations/`. Les données de référence régionales (règles d'échéance
et modèles) sont livrées dans `scripts/sql/seeds/` : s'adapter à un formulaire ou
à un délai réglementaire révisé est donc une modification de données, pas un
déploiement.

## Tests

La suite utilise [pytest](https://docs.pytest.org/) ; un conteneur PostgreSQL est
démarré automatiquement via `pytest-docker` :

```bash
pytest             # suite de tests
ruff check .       # linting
ruff format --check .
mypy .             # vérification de types
```

## Internationalisation

Les messages d'erreur de l'API sont traduits sous `locales/` en **anglais**,
**français**, **néerlandais** et **allemand**. La langue de la réponse est
sélectionnée à partir de l'en-tête `Accept-Language` de la requête.

## Contribuer

Les contributions sont les bienvenues ! Merci de lire le
[guide de contribution](../CONTRIBUTING.md) et notre
[code de conduite](../CODE_OF_CONDUCT.md) avant d'ouvrir une issue ou une pull
request.

## Sécurité

Pour signaler une vulnérabilité, veuillez suivre la
[politique de sécurité](../SECURITY.md) — n'ouvrez pas d'issue publique.

## Licence

Ce projet est distribué sous [licence Apache 2.0](../LICENSE).

Une exception : les formulaires CWaPE vierges fournis dans `document-templates/`
sont les documents propres au régulateur, redistribués sans modification, et ne
sont pas couverts par cette licence. Voir [NOTICE](../NOTICE).
