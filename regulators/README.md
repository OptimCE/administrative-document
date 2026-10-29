# Regulator plugins

Everything that belongs to one regulator lives here, in its plugin: its
procedures, their steps and deadlines, the pieces they need, the responses it
sends, its holidays. The engine (`engine/`) knows none of them. It reads a
plugin through `engine/contracts.py`, and the entry points hand it
`registry.REGISTRY`. `engine/` must never import this package;
`tests/engine/test_boundary.py` enforces that.

| Plugin | `community.regulator` | Region | Status |
| --- | --- | --- | --- |
| `cwape/` | `BE-WAL-CWAPE` | `WAL` | v1: 6 codes, 7 procedure types |
| `testing/fake.py` | `TEST-FAKE` | `TST` | the engine's test double, never registered |

Brugel (`BE-BRU-BRUGEL`) and the VREG (`BE-VLA-VREG`) have no plugin yet. A
community with either regulator gets no procedures: its events are
acknowledged with `reason="regulator_not_supported"`, and the dashboard says
so.

## Layout of a plugin

```
regulators/<name>/
  plugin.py              # the class implementing RegulatorPlugin
  catalogue/<CODE>.v<N>.yaml   # one file per procedure VERSION
  calendar/<REGION>.yaml       # the holidays its deadlines count on
tests/regulators/<name>/       # its tests (not here: the images copy this tree)
```

## Adding a regulator

1. **Calendar.** Write `calendar/<REGION>.yaml`: `region`, `saturday_is_working`,
   `years: {from, to}`, then one `{day, label}` per holiday. Every covered year
   needs at least one holiday. A deadline that reaches past `to` raises
   `CalendarNotCoveredError`, so cover at least a year beyond the present.
2. **Catalogue.** Write one `catalogue/<CODE>.v1.yaml` per procedure type. The
   model and its checks are in `engine/catalogue.py`; `cwape/catalogue/` is a
   complete example. Each file holds:
   - identity: `code`, `version`, `valid_from`, `scope` (community | operation),
     `recipient` (grd_ref | regulator), `channel` (email | online_form | post),
     `family` (one open draft per family);
   - routing: `handles_change_kinds` (the core's change kinds this procedure
     takes), `opens_annually` for a calendar procedure, and `variants` with
     `default_variant` and `escalation` (your own data, which only your
     `variant()` reads);
   - lifecycle: `initial_step` and `steps`. Each step has one of the engine's
     common statuses (draft, ready, sent, in_force, refused, cancelled, lapsed),
     a dashboard `bucket` (todo, waiting, planned, done) and a `next_action`
     (complete_documents, send, record_response, none) with its button label.
     `transitions` lists the edges; `automatic: true` marks the ones the engine
     takes itself;
   - `response_types`: what can be recorded, in or out, and the step each
     leads to;
   - `deadline_rules`: `relative` (an `anchor`, `step:<code>` or
     `response:<code>`, plus an `offset` in working_days, calendar_days or
     months) or `annual` (a day of the period's year). Also `party`,
     `deadline_kind`, and the events in `closes_on` that meet it;
   - `documents`: the pieces, each generated, collected or received. A piece
     can be `per_member`, have a `condition` your plugin evaluates, or be
     `required: false` (it does not hold the draft back).

   Every label comes in fr, en, nl and de. The frontend shows them as they are,
   so adding a regulator never touches it. Anything can be limited to some
   variants with `variants: [...]`.
3. **Plugin.** Implement `RegulatorPlugin` (`engine/contracts.py`): `code` (the
   `community.regulator` value), `region`, `catalogue()`, `calendar()` (load
   them with `load_catalogue` and `load_calendar`), and the four methods data
   cannot replace: `variant`, `required_documents`, `render_context` and
   `effects`. Codes of your own (steps, response types, variants, piece codes)
   are strings validated by the catalogue, never database enums. A regulator
   needs no migration.
4. **Register** it in `registry.py`. A plugin that is not listed there is not
   supported.
5. **Test** it in `tests/regulators/<name>/`. At least check the calendar
   against an independent source, the deadlines on real dates, `variant`, and
   that the whole catalogue syncs. See `tests/regulators/cwape/`.

## Changing a catalogue

Once a version is released, a running procedure follows it, so its file is
never edited for content. The catalogue sync (`engine/catalogue_sync.py`, run
at startup by the API and the worker) refuses to start on a released version
whose content changed, or whose file is gone.

- **New behaviour** (a step, a delay, a piece): add `<CODE>.v<N+1>.yaml` with a
  later `valid_from`. The previous version then ends the day before.
  Procedures keep the version they were opened under.
- **Wording**: edit the labels in place. Labels are excluded from
  `content_sha256`, and the sync refreshes them.
- **Holidays**: edit the calendar file. The sync makes `working_calendar`
  equal to it.
