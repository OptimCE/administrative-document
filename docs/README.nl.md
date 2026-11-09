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

De service levert twee deploybare onderdelen uit één codebase — een
[FastAPI](https://fastapi.tiangolo.com/)-API (`main:app`) en een NATS-worker
(`worker.main`) die de gebeurtenissen van het CRM in procedures onderbrengt, de
consumers voor generatie en resultaten draait, plus de dagelijkse taken —
gebouwd rond vier ideeën:

- **Procedures uit de catalogus van de regulator.** Een dossier is een
  *procedure* bij de regulator van de gemeenschap: het type, de stappen, de
  antwoorden, de documenten en de termijnen komen uit een geversioneerde
  YAML-catalogus in een regulatorplugin (`regulators/`; vandaag enkel de CWaPE,
  zie `regulators/README.md`). De motor (`engine/`) kent enkel gemeenschappelijke
  statussen — concept, klaar, verstuurd, van kracht, geweigerd, geannuleerd,
  vervallen — en de stap van de plugin reist met hen mee. Een status wordt nooit
  ter plaatse gewijzigd: elke wijziging voegt een onveranderlijk `status_event`
  toe, de database kopieert de kop van het journaal naar de procedure en weigert
  elke rechtstreekse schrijfbewerking. Een fout corrigeren is een nieuwe,
  traceerbare stapwijziging — de geschiedenis wordt nooit herschreven.
- **Wijzigingen erin, termijnen eruit.** Het CRM publiceert zijn wijzigingen (een
  toegevoegde meter, een goedgekeurde sleutel…) als gebeurtenissen; de worker
  brengt elke wijziging onder in het open concept van het juiste type, en de
  dagelijkse taken openen de kalenderprocedures (de jaarlijkse rapportering) en
  volgen hun termijnen op. Een termijn is catalogusdata — „N werkdagen of maanden
  na deze stap of dat antwoord" — geteld op de feestdagenkalender van de plugin,
  verschuivende feestdagen inbegrepen.
- **Onveranderlijke versies.** Elk geüpload of gegenereerd bestand wordt een
  genummerde, inhoudsgeadresseerde versie, net als elk verstuurd of ontvangen
  bericht. Zodra een procedure verstuurd is, liggen haar documenten vast, zodat
  je altijd precies kunt terugvinden wat werd overgemaakt.
- **Gegenereerde indieningen.** De verplichte formulieren komen voort uit de
  gegevens van de gemeenschap in plaats van met de hand ingevuld te worden: een
  voorinvulstap bouwt de payload op vanuit het CRM, de generatie bevriest die als
  momentopname en rendert de geregistreerde sjabloonbundel via de
  documentgeneratiedienst van het platform, en het resultaat komt binnen als een
  nieuwe onveranderlijke versie. De momentopname wordt bij de aanvraag vastgelegd,
  zodat wat werd ingediend altijd reproduceerbaar blijft. Per document kan maar
  één rendering tegelijk lopen.

Er worden twee **PostgreSQL**-databases gebruikt: de CRM-database
(gemeenschappen, leden, meters en abonnementen alleen-lezen, plus de
reglementaire status die deze service terugschrijft) en een lokale database met de
procedures, hun documenten, versies, correspondentie, het statusjournaal en de
termijnen. Opgeslagen bestanden staan in een **S3-compatibele objectopslag**
(MinIO in ontwikkeling). De gebeurtenissen van het CRM komen binnen via
**NATS JetStream**, en realtime signalen laten de open pagina's van de beheerders
vernieuwen. Traces, metrics en logs worden uitgestuurd via **OpenTelemetry**.

## Repositorystructuur

| Pad | Beschrijving |
|---|---|
| `api/` | HTTP-laag — procedureroutes (`/admin`), generatie, indieningen en sjablonen, health; schema's, services en repositories |
| `domain/` | Zuivere domeinlogica — voorinvulling van CRM naar formulier en de exacte woordenschat van de regulator |
| `engine/` | De proceduremotor — catalogusmodel en -synchronisatie, levenscyclus, onderbrengen van de CRM-gebeurtenissen, termijnen, dagelijkse taken; importeert nooit een regulator |
| `regulators/` | Regulatorplugins (CWaPE) — catalogus, feestdagenkalender, plugincontract; zie `regulators/README.md` |
| `ports/` | Adapters naar buiten — alleen-lezen CRM-toegang, NATS-eventpublicatie en de renderpoort naar documentgeneratie |
| `worker/` | NATS-consumers voor de CRM-gebeurtenissen, generatieaanvragen en hun resultaten, plus de dagelijkse taken |
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
beheerdersrol, schrijfacties op het sjabloonregister vereisen de
administratorrol, en `GET /filings/mine` is het enige endpoint dat voor leden
openstaat — het draagt geen rolbeperking omdat de service elke indiening tot de
eigen rijen van de aanroeper herleidt voordat het antwoord wordt opgebouwd.

| Methode | Pad | Doel |
|---|---|---|
| `GET` | `/admin/status` | De module voor deze gemeenschap: actief, regulator ondersteund of niet, te bevestigen beginsituatie |
| `GET` / `POST` | `/admin/baseline` · `/admin/baseline/confirm` | De te bevestigen beginsituatie, en de bevestiging ervan (schrijft de gevolgen in het CRM) |
| `GET` | `/admin/inbox` | Het dashboard: procedures per groep met hun volgende stap en de termijn ervan, en de drie dichtstbijzijnde termijnen |
| `GET` | `/admin/deadlines` | De agenda: alle open termijnen, of die van een periode (`from`, `to`) |
| `GET` / `POST` | `/admin/dossiers` | Alle procedures (gepagineerd, filterbaar), of er één met de hand openen vanuit de catalogus |
| `GET` | `/admin/dossiers/{id}` | Eén procedure: stappen, wijzigingen, documenten, termijnen, activiteit |
| `POST` | `/admin/dossiers/{id}/changes/{changeId}/defer` · `exclude` | Een wijziging uitstellen naar de volgende procedure, of ze met een reden uitsluiten |
| `POST` | `/admin/dossiers/{id}/documents/{pieceId}/versions` | Een document uploaden als nieuwe onveranderlijke versie (`multipart/form-data`) |
| `POST` | `/admin/dossiers/{id}/send` | Markeren als verstuurd (datum, kanaal, optioneel .eml of ontvangstbewijs); legt de procedure vast |
| `POST` | `/admin/dossiers/{id}/correspondence` | Een antwoord vastleggen; `dry_run=true` zegt wat het zou doen zonder iets te schrijven |
| `GET` | `/admin/dossiers/{id}/correspondence/{messageId}/file` | Het bestand van een bericht, zoals vastgelegd |
| `POST` | `/admin/dossiers/{id}/transition` | Een stapwijziging met de hand volgens de catalogus, met een reden |
| `GET` | `/admin/procedure-definitions` | De catalogus van de regulator, met labels in vier talen |
| `GET` | `/documents/{id}/prefill` | De payload van een document opbouwen vanuit het CRM, zonder iets op te slaan |
| `POST` | `/documents/{id}/generate` | De momentopname bevriezen en de rendering aanvragen (`409` als er al een loopt) |
| `GET` | `/documents/{id}/render-status` | De lopende rendering opvragen |
| `GET` | `/documents/{id}/versions/{versionId}/file` | Een opgeslagen versie downloaden |
| `GET` | `/filings/mine` | De eigen indieningen van de aanroeper, tot zijn rijen herleid vóór het antwoord wordt opgebouwd |
| `GET` | `/sharing-operations` | De deelverrichtingen van de gemeenschap, uit het CRM |
| `GET` / `POST` | `/templates` | Het sjabloonregister lezen (eigen overrides en platformstandaarden) of een override toevoegen |
| `PATCH` | `/templates/{id}` | De eigen override van de gemeenschap bewerken |

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
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0002_wal_templates.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0003_wal_templates_phase2.sql
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
`scripts/sql/migrations/`. De sjablooncatalogus wordt geleverd als seeds in
`scripts/sql/seeds/`, en elke regulatorplugin brengt zijn procedurecatalogus en
feestdagenkalender mee als YAML (`regulators/<plugin>/`), bij het opstarten met de
database gesynchroniseerd — zodat aanpassen aan een herzien formulier of een
gewijzigde termijn een gegevenswijziging is en geen codewijziging.

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
