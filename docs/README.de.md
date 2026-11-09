<p align="center">
  <img src="logo.svg" alt="OptimCE-Logo" width="160">
</p>

# OptimCE — Verwaltungsdokumente

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be)
[![Lizenz](https://img.shields.io/badge/Lizenz-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-43a047.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](README.nl.md)

**Verwaltungsdokumente** ist der Microservice für regulatorische Dossiers der
OptimCE-Plattform. Er ist die **administrative Quelle der Wahrheit** für die
Dokumente, die eine Energiegemeinschaft über ihren gesamten Lebenszyklus
erstellen, einreichen und nachverfolgen muss: Gründungsmeldung, jährliche
Berichterstattung, Teilungsgenehmigungen, Änderungen.

Er beantwortet die Fragen, die ein Papierprozess offenlässt: *Was genau wurde
übermittelt, wann, auf welcher Datengrundlage, und was ist die nächste Frist?*
Jede Statusänderung ist ein unveränderlicher Journaleintrag, jede gespeicherte
Datei eine unveränderliche Version, und regulatorische Fristen werden automatisch
aus diesen Übergängen abgeleitet, statt in einer Tabelle gepflegt zu werden.

OptimCE ist eine Open-Source-Plattform zur Verwaltung erneuerbarer
Energiegemeinschaften, entwickelt für den belgischen Kontext der Energieteilung.
Mehr über das Projekt erfahren Sie auf
[www.optimce.be](https://www.optimce.be). Dieser Dienst wird normalerweise als
Teil der vollständigen Plattform betrieben: siehe das
[Entwicklungs-Monorepo](https://github.com/OptimCE/monorepo), das alle
OptimCE-Dienste bündelt und die Docker-Compose-Umgebung bereitstellt.

## Funktionsweise

Der Dienst liefert zwei deploybare Einheiten aus einer Codebasis — eine
[FastAPI](https://fastapi.tiangolo.com/)-API (`main:app`) und einen NATS-Worker
(`worker.main`), der die Ereignisse des CRM in Verfahren einordnet, die Consumer
für Erzeugung und Ergebnisse sowie die täglichen Aufgaben ausführt — aufgebaut
um vier Ideen:

- **Verfahren aus dem Katalog der Regulierungsbehörde.** Ein Dossier ist ein
  *Verfahren* bei der Regulierungsbehörde der Gemeinschaft: Typ, Schritte,
  Antworten, Unterlagen und Fristen stammen aus einem versionierten YAML-Katalog
  in einem Regulierungs-Plugin (`regulators/`; derzeit nur die CWaPE, siehe
  `regulators/README.md`). Die Engine (`engine/`) kennt nur gemeinsame Status —
  Entwurf, bereit, gesendet, in Kraft, abgelehnt, storniert, verfallen — und der
  Schritt des Plugins wird mit ihnen geführt. Ein Status wird nie an Ort und
  Stelle geändert: Jede Änderung fügt ein unveränderliches `status_event` hinzu,
  die Datenbank überträgt den Kopf des Journals auf das Verfahren und lehnt jedes
  direkte Schreiben ab. Ein Fehler wird durch einen neuen, nachvollziehbaren
  Schrittwechsel korrigiert — die Historie wird nie umgeschrieben.
- **Änderungen hinein, Fristen heraus.** Das CRM veröffentlicht seine Änderungen
  (ein hinzugefügter Zähler, ein genehmigter Schlüssel…) als Ereignisse; der
  Worker ordnet jedes dem offenen Entwurf des passenden Typs zu, und die
  täglichen Aufgaben eröffnen die kalendarischen Verfahren (den Jahresbericht)
  und verfolgen ihre Fristen. Eine Frist ist Katalogdaten — „N Werktage oder
  Monate nach diesem Schritt oder jener Antwort" — gezählt nach dem
  Feiertagskalender des Plugins, bewegliche Feiertage inbegriffen.
- **Unveränderliche Versionen.** Jede hochgeladene oder erzeugte Datei wird zu
  einer nummerierten, inhaltsadressierten Version, ebenso jede gesendete oder
  empfangene Nachricht. Sobald ein Verfahren gesendet ist, sind seine Unterlagen
  eingefroren — so lässt sich stets genau abrufen, was übermittelt wurde.
- **Erzeugte Einreichungen.** Die vorgeschriebenen Formulare entstehen aus den
  Daten der Gemeinschaft, statt von Hand ausgefüllt zu werden: Ein
  Vorbefüllungsschritt baut die Nutzlast aus dem CRM, die Erzeugung friert sie
  als Momentaufnahme ein und rendert die registrierte Vorlage über den
  Dokumenterzeugungsdienst der Plattform, und das Ergebnis kommt als neue
  unveränderliche Version an. Die Momentaufnahme wird bei der Anfrage erfasst,
  sodass das Eingereichte stets reproduzierbar bleibt. Pro Unterlage kann nur
  ein Rendering gleichzeitig laufen.

Zwei **PostgreSQL**-Datenbanken kommen zum Einsatz: die CRM-Datenbank
(Gemeinschaften, Mitglieder, Zähler und Abonnements nur lesend, dazu der
regulatorische Status, den dieser Dienst zurückschreibt) und eine lokale
Datenbank für die Verfahren, ihre Unterlagen, Versionen, Korrespondenz, das
Statusjournal und die Fristen. Gespeicherte Dateien liegen in einem
**S3-kompatiblen Objektspeicher** (MinIO in der Entwicklung). Die Ereignisse des
CRM kommen über **NATS JetStream**, und Echtzeit-Hinweise lassen die offenen
Seiten der Verwalter neu laden. Traces, Metriken und Logs werden über
**OpenTelemetry** ausgegeben.

## Repository-Struktur

| Pfad | Beschreibung |
|---|---|
| `api/` | HTTP-Schicht — Verfahrensrouten (`/admin`), Erzeugung, Einreichungen und Vorlagen, Health; Schemata, Services und Repositories |
| `domain/` | Reine Domänenlogik — Vorbefüllung vom CRM ins Formular und das exakte Vokabular der Regulierungsbehörde |
| `engine/` | Die Verfahrens-Engine — Katalogmodell und -synchronisation, Lebenszyklus, Einordnung der CRM-Ereignisse, Fristen, tägliche Aufgaben; importiert nie eine Regulierungsbehörde |
| `regulators/` | Regulierungs-Plugins (CWaPE) — Katalog, Feiertagskalender, Plugin-Vertrag; siehe `regulators/README.md` |
| `ports/` | Adapter nach außen — lesender CRM-Zugriff, NATS-Event-Veröffentlichung und der Render-Port zur Dokumentenerzeugung |
| `worker/` | NATS-Consumer für die CRM-Ereignisse, Erzeugungsanfragen und deren Ergebnisse, dazu die täglichen Aufgaben |
| `document-templates/` | Die registrierten Vorlagen — die Formulare der Regulierungsbehörde und ihre Manifeste |
| `shared/` | Konstanten, Fehlerkatalog, ORM-Modelle, Hilfsfunktionen |
| `core/` | Querschnittsinfrastruktur — Konfiguration, Datenbank, Queue, Speicher, Sicherheit, Middleware, i18n, Tracing, Metriken, Logging |
| `tests/` | Testsuite (pytest) |
| `locales/` | Übersetzte API-Fehlermeldungen (en, fr, nl, de) |
| `scripts/` | Werkzeuge — OpenAPI-Export, SQL-Schema, Migrationen und Referenzdaten |

## API

Alle Endpunkte erfordern Authentifizierung und ein aktives Abonnement der
Gemeinschaft (siehe [Authentifizierung](#authentifizierung)). Das Gateway ergänzt
das externe Präfix `/administrative-document`; die folgenden Pfade sind daher
relativ.

Der Zugriff ist standardmäßig auf Verwalter beschränkt: Jede Route erfordert
mindestens die Verwalterrolle, Schreibzugriffe auf das Vorlagenregister erfordern
die Administratorrolle, und `GET /filings/mine` ist der einzige für Mitglieder
zugängliche Endpunkt — er trägt keine Rollenbeschränkung, weil der Dienst jede
Einreichung vor dem Aufbau der Antwort auf die Zeilen des Aufrufers reduziert.

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/admin/status` | Das Modul für diese Gemeinschaft: aktiv, Regulierungsbehörde unterstützt oder nicht, zu bestätigende Ausgangslage |
| `GET` / `POST` | `/admin/baseline` · `/admin/baseline/confirm` | Die zu bestätigende Ausgangslage und ihre Bestätigung (schreibt die Wirkungen ins CRM) |
| `GET` | `/admin/inbox` | Das Dashboard: Verfahren nach Gruppe mit nächstem Schritt und dessen Frist, dazu die drei nächsten Fristen |
| `GET` | `/admin/deadlines` | Die Agenda: alle offenen Fristen, oder die eines Zeitraums (`from`, `to`) |
| `GET` / `POST` | `/admin/dossiers` | Alle Verfahren (paginiert, filterbar), oder eines von Hand aus dem Katalog eröffnen |
| `GET` | `/admin/dossiers/{id}` | Ein Verfahren: Schritte, Änderungen, Unterlagen, Fristen, Verlauf |
| `POST` | `/admin/dossiers/{id}/changes/{changeId}/defer` · `exclude` | Eine Änderung auf das nächste Verfahren verschieben, oder sie mit Begründung ausschließen |
| `POST` | `/admin/dossiers/{id}/documents/{pieceId}/versions` | Eine Unterlage als neue unveränderliche Version hochladen (`multipart/form-data`) |
| `POST` | `/admin/dossiers/{id}/send` | Als gesendet markieren (Datum, Kanal, optional .eml oder Bestätigung); friert das Verfahren ein |
| `POST` | `/admin/dossiers/{id}/correspondence` | Eine Antwort erfassen; `dry_run=true` zeigt, was sie bewirken würde, ohne etwas zu schreiben |
| `GET` | `/admin/dossiers/{id}/correspondence/{messageId}/file` | Die Datei einer Nachricht, wie erfasst |
| `POST` | `/admin/dossiers/{id}/transition` | Ein Schrittwechsel von Hand entlang des Katalogs, mit Begründung |
| `GET` | `/admin/procedure-definitions` | Der Katalog der Regulierungsbehörde, mit Bezeichnungen in vier Sprachen |
| `GET` | `/documents/{id}/prefill` | Die Nutzlast einer Unterlage aus dem CRM aufbauen, ohne etwas zu speichern |
| `POST` | `/documents/{id}/generate` | Die Momentaufnahme einfrieren und das Rendering anfordern (`409`, wenn bereits eines läuft) |
| `GET` | `/documents/{id}/render-status` | Das laufende Rendering abfragen |
| `GET` | `/documents/{id}/versions/{versionId}/file` | Eine gespeicherte Version herunterladen |
| `GET` | `/filings/mine` | Die eigenen Einreichungen des Aufrufers, vor dem Aufbau der Antwort auf seine Zeilen reduziert |
| `GET` | `/sharing-operations` | Die Teilungsoperationen der Gemeinschaft, aus dem CRM |
| `GET` / `POST` | `/templates` | Das Vorlagenregister lesen (eigene Overrides und Plattformvorgaben) oder ein Override anlegen |
| `PATCH` | `/templates/{id}` | Das eigene Override der Gemeinschaft bearbeiten |

Health-Endpunkte liegen unter `/health` (`/health/liveness`,
`/health/readiness`, `/health/health`). Die interaktive OpenAPI-Dokumentation
(`/docs`, `/redoc`, `/openapi.json`) ist nur bei `ENV=local` aktiv.

### Authentifizierung

Der Dienst führt keine eigene Anmeldung durch. In der OptimCE-Plattform
authentifizieren ein [KrakenD](https://www.krakend.io/)-Gateway und
[Keycloak](https://www.keycloak.org/) die Anfrage und injizieren
Identitäts-Header (`x-user-id`, `x-community-id`, `x-user-groups`,
`x-user-orgs`). Die Rolle des Aufrufers wird für die aktive Gemeinschaft
aufgelöst, und der Zugriff auf die Funktion setzt ein aktives Abonnement voraus.

## Erste Schritte

### Voraussetzungen

- Docker und Docker Compose (empfohlen) **oder** Python 3.12 für die
  eigenständige lokale Entwicklung

### Über den OptimCE-Stack (empfohlen)

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

Der Dienst läuft als `administrative-document`:

```bash
curl http://localhost:8006/health/readiness
```

### Eigenständig

```bash
git clone https://github.com/OptimCE/administrative-document.git
cd administrative-document
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env
```

Wenden Sie Schema und Referenzdaten an und starten Sie die API (NATS, MinIO und
PostgreSQL müssen erreichbar sein — der Monorepo-Stack ist der einfachste Weg):

```bash
psql "$LOCAL_DATABASE_URL" -f scripts/sql/schema.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0002_wal_templates.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0003_wal_templates_phase2.sql
uvicorn main:app --reload
```

## Konfiguration

Die Konfiguration wird aus der Umgebung gelesen; `.env.exemple` dokumentiert jede
Variable. Die wichtigsten Gruppen sind:

- **CRM-Datenbank** (`CRM_DATABASE_URL`, `CRM_DB_*` Pool-Einstellungen)
- **Lokale Datenbank** (`LOCAL_DATABASE_URL`, `LOCAL_DB_*` Pool-Einstellungen)
- **Messaging** (`NATS_URL`)
- **Objektspeicher** (`STORAGE_ENDPOINT`, `STORAGE_BUCKET`,
  `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, `STORAGE_REGION`, `OUTPUT_BUCKET`)
- **CORS** (`ALLOW_ORIGIN`)
- **Observability** (`LOGGING_TOKEN`, `LOGGING_TRACES_URL`, `LOGGING_LOGS_URL`,
  `LOGGING_METRICS_URL`)
- **Umgebungsauswahl** (`ENV`: `local`, `test`, `staging`, `production`)

## Datenbankschema

Es gibt keinen Migrations-Runner. `scripts/sql/schema.sql` ist die einzige Quelle
der Wahrheit für die lokale Datenbank und wird von
`shared/models/local_models.py` gespiegelt; Änderungen kommen als
vorwärtsgerichtete Dateien nach `scripts/sql/migrations/`. Der Vorlagenkatalog
liegt als Seeds in `scripts/sql/seeds/`, und jedes Regulierungs-Plugin bringt
seinen Verfahrenskatalog und Feiertagskalender als YAML mit
(`regulators/<plugin>/`), beim Start mit der Datenbank synchronisiert — so ist
die Anpassung an ein überarbeitetes Formular oder eine geänderte Frist eine
Datenänderung und keine Codeänderung.

## Tests

Die Testsuite nutzt [pytest](https://docs.pytest.org/); ein
PostgreSQL-Container wird automatisch über `pytest-docker` gestartet:

```bash
pytest             # Testsuite
ruff check .       # Linting
ruff format --check .
mypy .             # Typprüfung
```

## Internationalisierung

API-Fehlermeldungen sind unter `locales/` auf **Englisch**, **Französisch**,
**Niederländisch** und **Deutsch** übersetzt. Die Antwortsprache ergibt sich aus
dem `Accept-Language`-Header der Anfrage.

## Mitwirken

Beiträge sind willkommen! Bitte lesen Sie die
[Beitragsrichtlinien](../CONTRIBUTING.md) und unseren
[Verhaltenskodex](../CODE_OF_CONDUCT.md), bevor Sie ein Issue oder einen Pull
Request eröffnen.

## Sicherheit

Um eine Sicherheitslücke zu melden, folgen Sie bitte der
[Sicherheitsrichtlinie](../SECURITY.md) — öffnen Sie kein öffentliches Issue.

## Lizenz

Dieses Projekt steht unter der [Apache-Lizenz 2.0](../LICENSE).

Eine Ausnahme: Die unter `document-templates/` mitgelieferten leeren
CWaPE-Formulare sind die eigenen Dokumente der Regulierungsbehörde, unverändert
weiterverbreitet, und fallen nicht unter diese Lizenz. Siehe
[NOTICE](../NOTICE).
