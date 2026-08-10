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

Der Dienst liefert zwei Deployables aus einer Codebasis — eine
[FastAPI](https://fastapi.tiangolo.com/)-API (`main:app`) und einen NATS-Worker
(`worker.main`), der die Consumer für Erzeugung und Ergebnisse sowie den
Fristenplaner ausführt — und beruht auf vier Ideen:

- **Ein journalisierter Zustandsautomat.** Dokumente durchlaufen
  `Entwurf → bereit → übermittelt → bestätigt` (plus `veraltet`); Dossiers
  durchlaufen `in Vorbereitung → eingereicht → vollständig → abgeschlossen` (plus
  `verfallen`). Ein Status wird nie an Ort und Stelle geändert: jede Änderung
  hängt ein unveränderliches `status_event` an (Akteur, Zeitstempel, Ausgangs-
  und Zielstatus, Kontext), und die Statusspalte ist nur ein Cache, den die
  Datenbank gegen dieses Journal prüft. Ein Fehler wird durch einen neuen,
  nachvollziehbaren Korrekturübergang berichtigt — die Historie wird nie
  umgeschrieben.
- **Abgeleitete Fristen.** Regulatorische Fristen sind Konfiguration, nicht Code.
  Eine `deadline_rule`-Zeile besagt: „Wenn *dies* geschieht, wird eine Frist
  *dieses Typs* N Werktage oder N Monate später fällig"; ein Übergang wertet die
  passenden Regeln aus und materialisiert die Fristen. Die Werktagsberechnung
  berücksichtigt die belgischen Feiertage einschließlich der beweglichen Feste.
- **Unveränderliche Versionen.** Jede hochgeladene Datei wird zu einer
  nummerierten, inhaltsadressierten Version. Ein erneuter Upload erzeugt eine neue
  Version; eine bereits übermittelte Version kann nie verändert werden, sodass
  sich stets exakt das abrufen lässt, was eingereicht wurde.
- **Erzeugte Einreichungen.** Die vorgeschriebenen Formulare entstehen aus den
  Daten der Gemeinschaft, statt von Hand ausgefüllt zu werden: Ein Prefill-Schritt
  baut die Nutzdaten aus dem CRM auf, die Erzeugung friert sie als Momentaufnahme
  ein und rendert die registrierte Vorlage über den Dokumentgenerierungsdienst der
  Plattform, und das Ergebnis landet als neue unveränderliche Version. Die
  Momentaufnahme entsteht zum Zeitpunkt der Anfrage, sodass das Eingereichte stets
  reproduzierbar bleibt. Pro Dokument kann nur ein Rendering gleichzeitig laufen.

Zwei **PostgreSQL**-Datenbanken kommen zum Einsatz: die CRM-Datenbank (nur lesend
für Gemeinschaften, Mitglieder, Zähler und Abonnements) und eine lokale
Datenbank, die Dossiers, Dokumente, Versionen, das Statusjournal und die Fristen
besitzt. Gespeicherte Dateien liegen in einem **S3-kompatiblen** Objektspeicher
(MinIO in der Entwicklung). Status- und Fristereignisse werden auf **NATS
JetStream** veröffentlicht. Traces, Metriken und Logs laufen über
**OpenTelemetry**.

## Repository-Struktur

| Pfad | Beschreibung |
|---|---|
| `api/` | HTTP-Schicht — Health- und Verwaltungsdokument-Routen, Schemata, Service und Repository |
| `domain/` | Reine Domänenlogik — Zustandsautomat, Fristenregel-Engine und belgischer Werktagskalender |
| `ports/` | Adapter nach außen — lesender CRM-Zugriff, NATS-Event-Veröffentlichung und der Render-Port zur Dokumentenerzeugung |
| `worker/` | NATS-Consumer für Erzeugungsanfragen und deren Ergebnisse, dazu der tägliche Fristenplaner |
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
mindestens die Verwalterrolle, Schreibzugriffe auf die Vorlagen- und
Fristenregelregister erfordern die Administratorrolle, und `GET /filings/mine` ist
der einzige für Mitglieder offene Endpunkt — er trägt keine Rollenprüfung, weil
der Dienst jedes Dossier auf die eigenen Zeilen des Aufrufers reduziert, bevor die
Antwort gebaut wird.

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` / `POST` | `/dossiers` | Dossiers auflisten (paginiert, filterbar) oder anlegen |
| `GET` / `PATCH` | `/dossiers/{id}` | Ein Dossier mit Dokumenten und Fristen lesen oder Referenz/Metadaten ändern |
| `POST` | `/dossiers/{id}/transition` · `rollback` | Eine Statusänderung des Dossiers erfassen, oder eine Korrektur |
| `GET` | `/dossiers/{id}/timeline` | Das unveränderliche Journal des Dossiers und seiner Dokumente |
| `GET` | `/dossiers/{id}/deadlines` | Die für ein Dossier abgeleiteten Fristen |
| `GET` / `POST` | `/dossiers/{id}/documents` | Dokumente auflisten oder hinzufügen |
| `GET` | `/documents/{id}` | Ein Dokument mit seinen Versionen lesen |
| `POST` | `/documents/{id}/versions` | Eine neue unveränderliche Version hochladen (`multipart/form-data`) |
| `GET` | `/documents/{id}/versions/{versionId}/file` | Eine gespeicherte Version herunterladen |
| `POST` | `/documents/{id}/transition` | Eine Statusänderung des Dokuments erfassen |
| `POST` | `/documents/{id}/mark-ready` · `mark-sent` · `acknowledge` · `rollback` | Kurzbefehle für die häufigen Übergänge |
| `GET` | `/documents/{id}/prefill` | Die Nutzdaten aus dem CRM aufbauen, ohne etwas zu speichern |
| `POST` | `/documents/{id}/generate` | Die Momentaufnahme einfrieren und das Rendering anfordern (`409`, wenn bereits eines läuft) |
| `GET` | `/documents/{id}/render-status` | Das laufende Rendering abfragen |
| `GET` | `/deadlines` | Fristen-Dashboard über alle Dossiers hinweg |
| `POST` | `/deadlines/{id}` | Eine Frist als erfüllt oder storniert markieren |
| `GET` | `/filings/mine` | Die eigenen Einreichungen des Aufrufers, vor dem Aufbau der Antwort auf seine Zeilen reduziert |
| `GET` | `/sharing-operations` | Die CRM-Sharing-Vorgänge, an die ein Dossier angehängt werden kann |
| `GET` / `POST` | `/templates`, `/deadline-rules` | Die Register lesen (eigene Overrides und Plattformvorgaben) oder ein Override anlegen |
| `PATCH` | `/templates/{id}`, `/deadline-rules/{id}` | Das eigene Override der Gemeinschaft bearbeiten |
| `POST` | `/maintenance/deadline-sweep` | Den Fristenlauf außerplanmäßig auslösen |

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
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0001_wal_deadline_rules.sql
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
vorwärtsgerichtete Dateien nach `scripts/sql/migrations/`. Regionale
Referenzdaten (Fristenregeln und Vorlagen) liegen in `scripts/sql/seeds/`, sodass
die Anpassung an ein überarbeitetes Formular oder eine geänderte Frist eine
Datenänderung ist und kein Deployment.

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
