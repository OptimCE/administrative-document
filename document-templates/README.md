# Rendering bundles

Each directory under `administrative-document/<doc_type>/v<n>/` is one template
bundle: a `manifest.json` plus the entrypoint document. `minio-init` copies the
tree into the `TEMPLATES_BUCKET`, and `document_template.file_ref` points at the
versioned prefix — so a bundle is addressed as
`s3://optimce-templates/administrative-document/<doc_type>/v<n>/`.

**This service never opens these files.** `document-generation` fetches and
renders them; the only thing here that reads them is
`tests/test_template_bundles.py`, which proves a manifest still agrees with the
document it wraps.

## The documents are the regulator's own

Every entrypoint is an unmodified CWaPE file. The PDFs are the authority's
fillable AcroForms and the workbooks are its mandated spreadsheets: rendering
sets field and cell values and changes nothing else, so what gets filed **is**
the official document rather than a reproduction of it. Each manifest records
which CWaPE publication and format date it wraps, in `_source`.

That also means they are **third-party material**. The `template.pdf` /
`template.xlsx` / `template.docx` files are not OptimCE-authored and are **not**
covered by this repository's Apache-2.0 licence; rights in them remain with the
regulator. The manifests beside them — the field and cell bindings, the JSON
Schema over `data`, the declared capacities — are OptimCE's own work and are
covered. See [NOTICE](../NOTICE) at the repository root.

## What a manifest declares

| key | engines | meaning |
|---|---|---|
| `blocks` | `xlsx` | where a variable-length list lands: sheet, anchor cell, column order |
| `fields` | `pdf-form` | data key → AcroForm field name; a **list** spells a value one character per comb box |
| `required_fields` | all | Draft 2020-12 schema over `data`; `maxItems` is the form's real row capacity |

Capacities are read off the workbooks' own data-validation ranges, not guessed —
100 members and 50 production units on the notification annex, 100 delivery
points and 20+20 units on the sharing annex. Exceeding one is a **permanent**
validation error: CWaPE defines no way to split a filing, and truncating would
file incomplete legal data.

Not every AcroForm field is mapped. The unmapped ones are left untouched and the
produced artifact is still a fillable PDF, so anything OptimCE cannot derive
stays editable by hand rather than being lost.

## Registering a revised form

A regulator revision is a reviewed, versioned change — never an in-place edit:

1. author the new bundle under `<doc_type>/v<n+1>/`;
2. `INSERT` a `document_template` row with the next `version` and the new
   `file_ref`, and set the previous row's `valid_to`;
3. re-run `tests/test_template_bundles.py` — it will fail if a field name or a
   capacity moved.

**Never mutate a published prefix.** `document-generation` caches bundles on disk
keyed by URI, with no TTL and no invalidation, so re-uploading to the same
versioned prefix has no effect until the container is recreated
(`up --force-recreate document-generation`).

## The DSO conventions (`docx`)

`dso_agreement_community` / `dso_agreement_building` (CWaPE 5614 / 5613) are Word
contracts. The regulator writes them with `[À compléter]` markers where the
parties fill in the deal; authoring the template replaced each marker with a
`{{ data.x }}` expression and **changed nothing else** — legal text, numbering,
styles and annex references are untouched.

Two consequences worth knowing:

- **Every field is required.** docxtpl runs with `StrictUndefined`, so a key the
  caller forgets is a hard error rather than a blank in a signed contract. A key
  supplied as `null` renders empty — the visible blank a reviewer fills in.
- **Both preamble variants survive.** The conventions offer an "OU" alternative
  (with/without a suspensive condition). Choosing between them is a legal
  decision, and the artifact is a `.docx` the parties still edit and sign, so the
  template keeps both rather than guessing.

Re-authoring after a revision is positional, not literal: the markers are matched
as bracketed blocks and the paragraph is rebuilt run-by-run, because Word splits
text across runs and these documents use French typography (U+2019 apostrophes,
no-break spaces before `:`) that makes exact-string matching a coin flip.
