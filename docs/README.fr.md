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
(`worker.main`) qui range les événements du CRM dans des démarches, exécute les
consommateurs de génération et de résultats ainsi que les tâches quotidiennes —
construits autour de quatre idées :

- **Des démarches tirées du catalogue du régulateur.** Un dossier est une
  *démarche* auprès du régulateur de la communauté : son type, ses étapes, ses
  réponses, ses pièces et ses délais viennent d'un catalogue YAML versionné dans
  un plugin de régulateur (`regulators/` ; la CWaPE est le seul aujourd'hui, voir
  `regulators/README.md`). Le moteur (`engine/`) ne connaît que des statuts
  communs — brouillon, prêt, envoyé, en vigueur, refusé, annulé, caduc — et
  l'étape du plugin voyage avec eux. Un statut n'est jamais modifié sur place :
  chaque changement ajoute un `status_event` immuable, la base recopie la tête du
  journal sur la démarche et refuse toute écriture directe. Corriger une erreur
  consiste à enregistrer un nouveau changement d'étape tracé — l'historique n'est
  jamais réécrit.
- **Des changements en entrée, des échéances en sortie.** Le CRM publie ses
  changements (un compteur ajouté, une clé approuvée…) sous forme d'événements ;
  le worker range chacun dans le brouillon ouvert du bon type, et les tâches
  quotidiennes ouvrent les démarches calendaires (le rapportage annuel) et
  suivent leurs échéances. Un délai est une donnée du catalogue — « N jours
  ouvrables ou N mois après telle étape ou telle réponse » — compté sur le
  calendrier des jours fériés du plugin, fêtes mobiles comprises.
- **Des versions immuables.** Chaque fichier téléversé ou généré devient une
  version numérotée, adressée par son empreinte, tout comme chaque message
  envoyé ou reçu. Une fois la démarche envoyée, ses pièces sont figées, ce qui
  garantit de toujours pouvoir récupérer exactement ce qui a été transmis.
- **Des dépôts générés.** Les formulaires obligatoires sont produits à partir des
  données de la communauté plutôt que remplis à la main : une étape de
  préremplissage construit la charge utile depuis le CRM, la génération fige
  celle-ci en instantané et effectue le rendu du modèle enregistré via le service
  de génération de documents de la plateforme, et le résultat arrive comme une
  nouvelle version immuable. L'instantané est capturé au moment de la demande, ce
  qui rend toujours reproductible ce qui a été déposé. Un seul rendu peut être en
  cours par pièce.

Deux bases **PostgreSQL** sont utilisées : la base du CRM (communautés, membres,
compteurs et abonnements en lecture seule, plus le statut réglementaire que ce
service y écrit) et une base locale qui détient les démarches, leurs pièces, les
versions, la correspondance, le journal de statuts et les échéances. Les fichiers
stockés résident dans un stockage objet **compatible S3** (MinIO en
développement). Les événements du CRM arrivent par **NATS JetStream**, et des
signaux en temps réel demandent aux pages ouvertes des gestionnaires de se
rafraîchir. Traces, métriques et journaux sont émis via **OpenTelemetry**.

## Structure du dépôt

| Chemin | Description |
|---|---|
| `api/` | Couche HTTP — routes des démarches (`/admin`), génération, dépôts et modèles, santé ; schémas, services et repositories |
| `domain/` | Logique métier pure — préremplissage CRM vers formulaire et vocabulaire exact du régulateur |
| `engine/` | Le moteur de démarches — modèle et synchronisation du catalogue, cycle de vie, rangement des événements du CRM, échéances, tâches quotidiennes ; n'importe jamais un régulateur |
| `regulators/` | Plugins de régulateur (CWaPE) — catalogue, calendrier des jours fériés, contrat du plugin ; voir `regulators/README.md` |
| `ports/` | Adaptateurs vers l'extérieur — lecture seule du CRM, publication d'événements NATS et port de rendu vers la génération de documents |
| `worker/` | Consommateurs NATS des événements du CRM, des demandes de génération et de leurs résultats, plus les tâches quotidiennes |
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
rôle gestionnaire, les écritures dans le registre des modèles exigent le rôle
administrateur, et `GET /filings/mine` est l'unique endpoint ouvert aux membres —
il ne porte aucune restriction de rôle parce que le service réduit chaque dépôt
aux seules lignes de l'appelant avant de construire la réponse.

| Méthode | Chemin | Rôle |
|---|---|---|
| `GET` | `/admin/status` | Le module pour cette communauté : actif, régulateur suivi ou non, état de départ à confirmer |
| `GET` / `POST` | `/admin/baseline` · `/admin/baseline/confirm` | L'état de départ à confirmer, et sa confirmation (écrit les effets dans le CRM) |
| `GET` | `/admin/inbox` | Le tableau de bord : démarches par groupe avec leur prochaine action et son échéance, et les trois échéances les plus proches |
| `GET` | `/admin/deadlines` | L'agenda : toutes les échéances ouvertes, ou celles d'une période (`from`, `to`) |
| `GET` / `POST` | `/admin/dossiers` | Toutes les démarches (paginé, filtrable), ou en ouvrir une à la main depuis le catalogue |
| `GET` | `/admin/dossiers/{id}` | Une démarche : étapes, changements, pièces, échéances, fil |
| `POST` | `/admin/dossiers/{id}/changes/{changeId}/defer` · `exclude` | Reporter un changement à la démarche suivante, ou l'exclure avec un motif |
| `POST` | `/admin/dossiers/{id}/documents/{pieceId}/versions` | Téléverser une pièce comme nouvelle version immuable (`multipart/form-data`) |
| `POST` | `/admin/dossiers/{id}/send` | Marquer comme envoyé (date, canal, .eml ou accusé facultatif) ; fige la démarche |
| `POST` | `/admin/dossiers/{id}/correspondence` | Enregistrer une réponse ; `dry_run=true` indique ce qu'elle ferait sans rien écrire |
| `GET` | `/admin/dossiers/{id}/correspondence/{messageId}/file` | Le fichier d'un message, tel qu'enregistré |
| `POST` | `/admin/dossiers/{id}/transition` | Un changement d'étape à la main selon le catalogue, avec son motif |
| `GET` | `/admin/procedure-definitions` | Le catalogue du régulateur, avec ses libellés en quatre langues |
| `GET` | `/documents/{id}/prefill` | Construire la charge utile d'une pièce depuis le CRM, sans rien persister |
| `POST` | `/documents/{id}/generate` | Figer l'instantané et demander le rendu (`409` si un rendu est déjà en cours) |
| `GET` | `/documents/{id}/render-status` | Interroger le rendu en cours |
| `GET` | `/documents/{id}/versions/{versionId}/file` | Télécharger une version stockée |
| `GET` | `/filings/mine` | Les dépôts propres à l'appelant, réduits à ses lignes avant construction de la réponse |
| `GET` | `/sharing-operations` | Les opérations de partage de la communauté, depuis le CRM |
| `GET` / `POST` | `/templates` | Consulter le registre des modèles (surcharges propres et valeurs par défaut) ou ajouter une surcharge |
| `PATCH` | `/templates/{id}` | Modifier la surcharge propre à la communauté |

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
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0002_wal_templates.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0003_wal_templates_phase2.sql
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
`scripts/sql/migrations/`. Le catalogue des modèles est livré dans
`scripts/sql/seeds/`, et chaque plugin de régulateur porte son catalogue de
démarches et son calendrier des jours fériés en YAML (`regulators/<plugin>/`),
synchronisés avec la base au démarrage : s'adapter à un formulaire ou à un délai
révisé est donc une modification de données, pas de code.

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
