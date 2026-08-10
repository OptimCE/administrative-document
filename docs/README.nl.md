<p align="center">
  <img src="logo.svg" alt="OptimCE-logo" width="160">
</p>

# OptimCE — Administratieve documenten

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be)
[![Licentie](https://img.shields.io/badge/Licentie-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-43a047.svg)](README.nl.md)

**Administratieve documenten** is de microservice voor reglementaire dossiers van
het OptimCE-platform. Het vormt de **administratieve bron van waarheid** voor de
documenten die een energiegemeenschap gedurende haar hele levenscyclus moet
opstellen, indienen en opvolgen: kennisgeving van oprichting, jaarlijkse
rapportering, deelmachtigingen, wijzigingen.

Het beantwoordt de vragen die een papieren proces niet beantwoordt: *wat is
precies ingediend, wanneer, op basis van welke gegevens, en wat is de volgende
termijn?* Elke statuswijziging is een onveranderlijke journaalregel, elk
opgeslagen bestand is een onveranderlijke versie, en reglementaire termijnen
worden automatisch afgeleid uit die overgangen in plaats van te worden bijgehouden
in een rekenblad.

OptimCE is een opensourceplatform voor het beheer van hernieuwbare
energiegemeenschappen, gebouwd voor de Belgische context van energiedeling. Meer
informatie over het project vindt u op
[www.optimce.be](https://www.optimce.be). Deze service draait normaal als
onderdeel van het volledige platform: zie de
[ontwikkelmonorepo](https://github.com/OptimCE/monorepo), die alle
OptimCE-services samenbrengt en de Docker Compose-omgeving levert om ze samen uit
te voeren.

## Werking

De service levert twee deployables uit één codebase — een
[FastAPI](https://fastapi.tiangolo.com/)-API (`main:app`) en een NATS-worker
(`worker.main`) die de consumers voor generatie en resultaten draait, plus de
termijnplanner — opgebouwd rond vier ideeën:

- **Een gejournaliseerde toestandsmachine.** Documenten doorlopen
  `concept → gereed → verzonden → ontvangstbevestigd` (plus `verouderd`);
  dossiers doorlopen `in voorbereiding → ingediend → volledig → afgesloten` (plus
  `vervallen`). Een status wordt nooit ter plaatse aangepast: elke wijziging voegt
  een onveranderlijke `status_event` toe (auteur, tijdstip, bron- en doelstatus,
  context), en de statuskolom is slechts een cache die de database toetst aan dat
  journaal. Een fout corrigeren gebeurt via een nieuwe, getraceerde corrigerende
  overgang — de geschiedenis wordt nooit herschreven.
- **Afgeleide termijnen.** Reglementaire termijnen zijn configuratie, geen code.
  Een `deadline_rule`-regel zegt: "wanneer *dit* gebeurt, valt een termijn *van
  dat type* N werkdagen of N maanden later"; een overgang evalueert de
  overeenkomende regels en materialiseert de termijnen. De werkdagberekening houdt
  rekening met de Belgische feestdagen, inclusief de beweeglijke feesten.
- **Onveranderlijke versies.** Elk geüpload bestand wordt een genummerde versie
  met inhoudsadressering. Opnieuw uploaden maakt een nieuwe versie aan; een
  reeds verzonden versie kan nooit worden gewijzigd, zodat u altijd exact kunt
  ophalen wat werd ingediend.
- **Gegenereerde indieningen.** De verplichte formulieren komen voort uit de
  gegevens van de gemeenschap in plaats van met de hand te worden ingevuld: een
  prefill-stap bouwt de payload op uit het CRM, de generatie bevriest die als
  momentopname en rendert het geregistreerde sjabloon via de
  documentgeneratieservice van het platform, en het resultaat belandt als nieuwe
  onveranderlijke versie. De momentopname wordt genomen op het moment van de
  aanvraag, zodat wat werd ingediend altijd reproduceerbaar is. Er kan per
  document maar één rendering tegelijk lopen.

Er worden twee **PostgreSQL**-databases gebruikt: de CRM-database (alleen-lezen
bron voor gemeenschappen, leden, meters en abonnementen) en een lokale database
die de dossiers, documenten, versies, het statusjournaal en de termijnen bezit.
Opgeslagen bestanden staan in een **S3-compatibele** objectopslag (MinIO in
ontwikkeling). Status- en termijngebeurtenissen worden gepubliceerd op **NATS
JetStream**. Traces, metrieken en logs verlopen via **OpenTelemetry**.

## Repositorystructuur

| Pad | Beschrijving |
|---|---|
| `api/` | HTTP-laag — health- en administratieve documentroutes, schema's, service en repository |
| `domain/` | Zuivere domeinlogica — de toestandsmachine, de termijnregelmotor en de Belgische werkdagkalender |
| `ports/` | Adapters naar buiten — alleen-lezen CRM-toegang, NATS-eventpublicatie en de renderpoort naar documentgeneratie |
| `worker/` | NATS-consumers voor generatieaanvragen en hun resultaten, plus de dagelijkse termijnplanner |
| `document-templates/` | De geregistreerde sjablonen — de eigen formulieren van de regulator en hun manifesten |
| `shared/` | Constanten, foutcatalogus, ORM-modellen, hulpfuncties |
| `core/` | Overkoepelende infrastructuur — configuratie, database, queue, opslag, beveiliging, middleware, i18n, tracing, metrieken, logging |
| `tests/` | Testsuite (pytest) |
| `locales/` | Vertaalde API-foutmeldingen (en, fr, nl, de) |
| `scripts/` | Hulpmiddelen — OpenAPI-export, SQL-schema, migraties en referentiegegevens |

## API

Alle endpoints vereisen authenticatie en een actief abonnement van de gemeenschap
(zie [Authenticatie](#authenticatie)). De gateway voegt het externe voorvoegsel
`/administrative-document` toe; onderstaande paden zijn dus relatief.

Toegang is standaard voorbehouden aan beheerders: elke route vereist minstens de
beheerdersrol, schrijfacties op de sjabloon- en termijnregelregisters vereisen de
administratorrol, en `GET /filings/mine` is het enige endpoint dat voor leden
openstaat — het draagt geen rolcontrole omdat de service elk dossier tot de eigen
rijen van de aanroeper herleidt vóór het antwoord wordt opgebouwd.

| Methode | Pad | Doel |
|---|---|---|
| `GET` / `POST` | `/dossiers` | Dossiers lijsten (gepagineerd, filterbaar) of openen |
| `GET` / `PATCH` | `/dossiers/{id}` | Een dossier met documenten en termijnen lezen, of referentie/metadata aanpassen |
| `POST` | `/dossiers/{id}/transition` · `rollback` | Een statuswijziging van het dossier vastleggen, of een correctie |
| `GET` | `/dossiers/{id}/timeline` | Het onveranderlijke journaal van het dossier en zijn documenten |
| `GET` | `/dossiers/{id}/deadlines` | De voor één dossier afgeleide termijnen |
| `GET` / `POST` | `/dossiers/{id}/documents` | Documenten lijsten of toevoegen |
| `GET` | `/documents/{id}` | Een document met zijn versies lezen |
| `POST` | `/documents/{id}/versions` | Een nieuwe onveranderlijke versie uploaden (`multipart/form-data`) |
| `GET` | `/documents/{id}/versions/{versionId}/file` | Een opgeslagen versie downloaden |
| `POST` | `/documents/{id}/transition` | Een statuswijziging van het document vastleggen |
| `POST` | `/documents/{id}/mark-ready` · `mark-sent` · `acknowledge` · `rollback` | Sneltoetsen voor de gangbare overgangen |
| `GET` | `/documents/{id}/prefill` | De payload uit het CRM opbouwen, zonder iets te bewaren |
| `POST` | `/documents/{id}/generate` | De momentopname bevriezen en de rendering aanvragen (`409` als er al één loopt) |
| `GET` | `/documents/{id}/render-status` | De lopende rendering opvragen |
| `GET` | `/deadlines` | Termijnendashboard over alle dossiers heen |
| `POST` | `/deadlines/{id}` | Een termijn als nagekomen of geannuleerd markeren |
| `GET` | `/filings/mine` | De eigen indieningen van de aanroeper, tot zijn rijen herleid vóór het antwoord wordt opgebouwd |
| `GET` | `/sharing-operations` | De CRM-deelbewerkingen waaraan een dossier kan worden gekoppeld |
| `GET` / `POST` | `/templates`, `/deadline-rules` | De registers lezen (eigen overrides en platformstandaarden) of een override toevoegen |
| `PATCH` | `/templates/{id}`, `/deadline-rules/{id}` | De eigen override van de gemeenschap bewerken |
| `POST` | `/maintenance/deadline-sweep` | De termijnsweep buiten de planning starten |

Health-endpoints staan onder `/health` (`/health/liveness`,
`/health/readiness`, `/health/health`). De interactieve OpenAPI-documentatie
(`/docs`, `/redoc`, `/openapi.json`) is alleen actief wanneer `ENV=local`.

### Authenticatie

De service voert zelf geen login uit. Binnen het OptimCE-platform
authenticeren een [KrakenD](https://www.krakend.io/)-gateway en
[Keycloak](https://www.keycloak.org/) het verzoek en injecteren zij
identiteitsheaders (`x-user-id`, `x-community-id`, `x-user-groups`,
`x-user-orgs`). De rol van de aanroeper wordt bepaald voor de actieve
gemeenschap, en toegang tot de functionaliteit vereist een actief abonnement.

## Aan de slag

### Vereisten

- Docker en Docker Compose (aanbevolen), **of** Python 3.12 voor zelfstandige
  lokale ontwikkeling

### Via de OptimCE-stack (aanbevolen)

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

De service draait als `administrative-document`:

```bash
curl http://localhost:8006/health/readiness
```

### Zelfstandig

```bash
git clone https://github.com/OptimCE/administrative-document.git
cd administrative-document
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env
```

Pas het schema en de referentiegegevens toe en start de API (NATS, MinIO en
PostgreSQL moeten bereikbaar zijn — de monorepo-stack is de eenvoudigste manier):

```bash
psql "$LOCAL_DATABASE_URL" -f scripts/sql/schema.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0001_wal_deadline_rules.sql
uvicorn main:app --reload
```

## Configuratie

De configuratie wordt uit de omgeving gelezen; `.env.exemple` documenteert elke
variabele. De belangrijkste groepen zijn:

- **CRM-database** (`CRM_DATABASE_URL`, `CRM_DB_*` poolinstellingen)
- **Lokale database** (`LOCAL_DATABASE_URL`, `LOCAL_DB_*` poolinstellingen)
- **Messaging** (`NATS_URL`)
- **Objectopslag** (`STORAGE_ENDPOINT`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY`,
  `STORAGE_SECRET_KEY`, `STORAGE_REGION`, `OUTPUT_BUCKET`)
- **CORS** (`ALLOW_ORIGIN`)
- **Observability** (`LOGGING_TOKEN`, `LOGGING_TRACES_URL`, `LOGGING_LOGS_URL`,
  `LOGGING_METRICS_URL`)
- **Omgevingsselectie** (`ENV`: `local`, `test`, `staging`, `production`)

## Databaseschema

Er is geen migratierunner. `scripts/sql/schema.sql` is de enige bron van waarheid
voor de lokale database en wordt weerspiegeld door
`shared/models/local_models.py`; wijzigingen komen als voorwaartse bestanden in
`scripts/sql/migrations/`. Regionale referentiegegevens (termijnregels en
sjablonen) worden geleverd in `scripts/sql/seeds/`, zodat aanpassen aan een
herzien formulier of een gewijzigde termijn een gegevenswijziging is en geen
deployment.

## Testen

De testsuite gebruikt [pytest](https://docs.pytest.org/); een
PostgreSQL-container wordt automatisch gestart via `pytest-docker`:

```bash
pytest             # testsuite
ruff check .       # linting
ruff format --check .
mypy .             # typecontrole
```

## Internationalisatie

API-foutmeldingen zijn vertaald onder `locales/` in het **Engels**, **Frans**,
**Nederlands** en **Duits**. De taal van het antwoord volgt uit de
`Accept-Language`-header van het verzoek.

## Bijdragen

Bijdragen zijn welkom! Lees de
[bijdragerichtlijnen](../CONTRIBUTING.md) en onze
[gedragscode](../CODE_OF_CONDUCT.md) voordat u een issue of pull request opent.

## Beveiliging

Volg voor het melden van een kwetsbaarheid het
[beveiligingsbeleid](../SECURITY.md) — open geen publieke issue.

## Licentie

Dit project valt onder de [Apache License 2.0](../LICENSE).

Eén uitzondering: de blanco CWaPE-formulieren in `document-templates/` zijn de
eigen documenten van de regulator, ongewijzigd meegeleverd, en vallen niet onder
deze licentie. Zie [NOTICE](../NOTICE).
