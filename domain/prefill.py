"""Turn CRM reads into the render payload for a document type.

Pure functions — no session, no broker, no clock — so the whole mapping is unit
tested against plain dataclasses.

Two properties make this module load-bearing rather than incidental:

* **One payload, two uses.** The dict built here is persisted verbatim to
  ``document_render.data_snapshot_json`` (and from there to
  ``document_version.data_snapshot_json``) *and* sent as the docgen ``data``.
  That identity is what makes spec R2 — "the filed version reflects the state at
  generation time" — true rather than an approximation reassembled afterwards.
* **User overrides win.** ``merge_overrides`` applies the reviewer's corrections
  on top of the CRM values before either use, so the snapshot records what was
  actually filed, not what the CRM happened to hold.

Keys are the *semantic* names the manifests bind to columns and AcroForm fields;
they are part of the bundle contract, so renaming one means bumping the affected
template version.
"""

from __future__ import annotations

import copy
import datetime
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from domain import cwape_labels as labels
from ports.crm_core import (
    WARNING_COMMUNITY_FIELD_MISSING,
    WARNING_EAN_NOT_FORM_REPRESENTABLE,
    WARNING_MEMBER_FIELD_MISSING,
    CommunityContext,
    Installation,
    Participant,
    PostalAddress,
    PrefillWarning,
)

# Document types this module knows how to pre-fill. A doc_type absent from here
# still generates — the reviewer simply starts from an empty form.
DOC_TYPE_ANNEX6_NOTIFICATION = "annex6_notification"
DOC_TYPE_ANNEX6_SHARING_FORM = "annex6_sharing_form"
DOC_TYPE_ANNUAL_PARTICIPANT_LIST = "annual_participant_list"
DOC_TYPE_ANNEX8_SWORN_DECLARATION = "annex8_sworn_declaration"
DOC_TYPE_ANNEX7_LEGAL_ENTITY = "annex7_legal_entity"
DOC_TYPE_COMMUNITY_NOTIFICATION = "community_notification"
DOC_TYPE_COMMUNITY_MODIFICATION = "community_modification"
DOC_TYPE_SHARING_NOTIFICATION = "sharing_notification"
DOC_TYPE_SHARING_MODIFICATION = "sharing_modification"
DOC_TYPE_DSO_AGREEMENT_COMMUNITY = "dso_agreement_community"
DOC_TYPE_DSO_AGREEMENT_BUILDING = "dso_agreement_building"

#: An EAN is exactly 18 digits. Mirrors crm-backend's
#: `modules/meters/shared/ean.ts` and crm-frontend's
#: `shared/validators/ean.validator.ts`; kept local because this is the only
#: Python service that derives anything from the EAN's shape, and `core/*` is
#: copy-pasted byte-identical across the annexes.
EAN_PATTERN = re.compile(r"^[0-9]{18}$")

#: The digits the CWaPE annex 8 pre-prints before its 16 one-character boxes.
#: A fact about the paper form, not a rule about EANs.
_EAN_FORM_PREFIX = "54"

#: (snapshot key the reviewer edits, comb key the bundle manifest binds).
_ANNEX8_COMB_FIELDS = (
    ("ean_delivery", "ean_delivery_digits"),
    ("ean_injection", "ean_injection_digits"),
)

#: The CWaPE DSO conventions are Word contracts rendered with StrictUndefined, so
#: every key must be present or the render fails hard. Most are negotiated terms
#: the CRM cannot know, so they are seeded blank for the reviewer — a visible
#: blank in a draft contract is exactly right, and far better than a guess.
_DSO_AGREEMENT_FIELDS = (
    "grid_operator_name",
    "representative_name",
    "sharing_description",
    "distribution_key",
    "start_date",
    "suspensive_condition",
    "signed_at",
    "signed_on",
)


def _iso(value: datetime.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def format_address(address: PostalAddress | None) -> str:
    """One-line address, for forms with a single address field."""
    if address is None:
        return ""
    head = " ".join(part for part in (address.street, address.number) if part)
    tail = " ".join(part for part in (address.postcode, address.city) if part)
    return ", ".join(part for part in (head, address.supplement, tail) if part)


#: Row key carrying the CRM member id, on every row of the three member-bearing
#: blocks below.
#:
#: Deliberately underscore-prefixed and NOT part of the bundle contract: the
#: xlsx renderer projects only the manifest's declared columns and the AcroForm
#: engine binds named fields, so an extra key changes no rendered output. It
#: exists so a member can be shown their own rows out of a frozen snapshot
#: without re-reading the CRM (which would show today's data, not what was
#: filed) and without matching on a display name (which collides on homonyms).
ROW_MEMBER_ID = "_id_member"

#: The snapshot keys whose value is a list of member-bearing rows.
MEMBER_ROW_BLOCKS = ("members", "participants", "installations", "storage")


def community_block(community: CommunityContext) -> dict[str, Any]:
    """The identity header every CWaPE form opens with."""
    address = community.address
    return {
        "community_name": community.name,
        "community_legal_name": community.legal_name or community.name,
        "community_vat_number": community.vat_number,
        "community_street": address.street if address else None,
        "community_number": address.number if address else None,
        "community_postcode": address.postcode if address else None,
        "community_city": address.city if address else None,
        "community_address": format_address(address),
    }


def participant_rows(participants: Sequence[Participant]) -> list[dict[str, Any]]:
    """Delivery-point rows for the sharing form ("points de prélèvement").

    One row per EAN, not per member: this sheet is a list of delivery points, so
    a member with three meters appears three times.
    """
    rows: list[dict[str, Any]] = []
    for participant in participants:
        is_company = participant.member_type == labels.MemberType.COMPANY
        for meter in participant.meters:
            rows.append(
                {
                    # Identity, not form content: no manifest binds it, and the
                    # renderers project only their declared columns. It is what
                    # lets `GET /filings/mine` return a member their own rows
                    # without re-reading the CRM or matching on a display name.
                    ROW_MEMBER_ID: participant.id_member,
                    "ean": meter.ean,
                    "meter_number": meter.meter_number,
                    "grd": labels.grd_label(meter.grd),
                    "nom": None if is_company else participant.name,
                    "prenom": None if is_company else participant.first_name,
                    "denomination": participant.name if is_company else None,
                    "rue": meter.address.street if meter.address else None,
                    "numero": meter.address.number if meter.address else None,
                    "code_postal": meter.address.postcode if meter.address else None,
                    "localite": meter.address.city if meter.address else None,
                    "client_residentiel": labels.residential_label(meter.client_type),
                    # A fixed distribution key is a per-operation decision the CRM
                    # does not hold; left blank for the reviewer.
                    "part_allouee": None,
                }
            )
    return rows


def member_rows(participants: Sequence[Participant]) -> list[dict[str, Any]]:
    """Member/shareholder rows for the notification form.

    One row per *member*, unlike ``participant_rows``: this sheet is a list of
    people, and their meters are irrelevant to it.
    """
    return [
        {
            ROW_MEMBER_ID: participant.id_member,
            "categorie": labels.member_category_label(participant.member_type),
            # Cascades from the category in the workbook; not derivable.
            "sous_categorie": None,
            "nom": labels.display_name(
                participant.name, participant.first_name, participant.member_type
            ),
            "numero_entreprise": participant.vat_number,
            "rue": participant.address.street if participant.address else None,
            "numero": participant.address.number if participant.address else None,
            "code_postal": participant.address.postcode if participant.address else None,
            "localite": participant.address.city if participant.address else None,
            # Shareholding is not modelled in the CRM.
            "droits_de_vote": None,
        }
        for participant in participants
    ]


def installation_rows(
    installations: Sequence[Installation], *, community_owned: bool = False
) -> list[dict[str, Any]]:
    """Production-unit rows.

    ``community_owned`` selects the producer-status vocabulary: the notification
    form describes the *community's* relationship to the installation, the
    sharing form the *producer's*. The two dropdowns are different lists.
    """
    status = (
        labels.community_installation_status_label
        if community_owned
        else labels.producer_status_label
    )
    is_company = labels.MemberType.COMPANY
    return [
        {
            ROW_MEMBER_ID: installation.id_member,
            "ean": installation.ean,
            "meter_number": installation.meter_number,
            "grd": labels.grd_label(installation.grd),
            "nom": None if installation.member_type == is_company else installation.member_name,
            "prenom": (
                None if installation.member_type == is_company else installation.member_first_name
            ),
            "denomination": (
                installation.member_name if installation.member_type == is_company else None
            ),
            "statut_producteur": status(installation.injection_status),
            "rue": installation.address.street if installation.address else None,
            "numero": installation.address.number if installation.address else None,
            "code_postal": installation.address.postcode if installation.address else None,
            "localite": installation.address.city if installation.address else None,
            # The CRM records no commissioning state; the reviewer picks one.
            "statut_installation": None,
            "date_mise_en_service": _iso(installation.commissioned_on),
            "puissance_kva": installation.generating_capacity,
            "filiere": labels.production_chain_label(installation.production_chain),
            # Percentage of injection allocated to sharing — an operation-level
            # decision, not a CRM fact.
            "part_injectee": None,
        }
        for installation in installations
    ]


def build_snapshot(
    doc_type: str,
    *,
    community: CommunityContext,
    operation_name: str | None = None,
    participants: Sequence[Participant] = (),
    production: Sequence[Installation] = (),
    storage: Sequence[Installation] = (),
) -> dict[str, Any]:
    """Assemble the render payload for ``doc_type``.

    An unknown doc_type still gets the community header — enough for a reviewer
    to work from, and better than refusing to open the form.
    """
    snapshot: dict[str, Any] = community_block(community)
    snapshot["operation_name"] = operation_name

    if doc_type in (DOC_TYPE_ANNEX6_NOTIFICATION, DOC_TYPE_ANNUAL_PARTICIPANT_LIST):
        snapshot["members"] = member_rows(participants)
        snapshot["installations"] = installation_rows(production, community_owned=True)
    elif doc_type == DOC_TYPE_ANNEX6_SHARING_FORM:
        snapshot["participants"] = participant_rows(participants)
        snapshot["installations"] = installation_rows(production)
        # The CRM does not model storage units; the reviewer adds any by hand.
        snapshot["storage"] = installation_rows(storage)
    elif doc_type in (
        DOC_TYPE_DSO_AGREEMENT_COMMUNITY,
        DOC_TYPE_DSO_AGREEMENT_BUILDING,
    ):
        # A negotiated contract: the CRM knows who the community is and nothing
        # about the terms. Every key is seeded so the render (StrictUndefined)
        # cannot fail on a missing one, and blank so the reviewer sees what is
        # theirs to write.
        for field in _DSO_AGREEMENT_FIELDS:
            snapshot[field] = None
        snapshot["representative_name"] = community.legal_name or community.name
        if doc_type == DOC_TYPE_DSO_AGREEMENT_COMMUNITY:
            snapshot["community_address"] = format_address(community.address)
        else:
            snapshot["unmet_condition"] = None
    elif doc_type in (
        DOC_TYPE_SHARING_NOTIFICATION,
        DOC_TYPE_SHARING_MODIFICATION,
        DOC_TYPE_COMMUNITY_NOTIFICATION,
        DOC_TYPE_COMMUNITY_MODIFICATION,
        DOC_TYPE_ANNEX8_SWORN_DECLARATION,
        DOC_TYPE_ANNEX7_LEGAL_ENTITY,
    ):
        # AcroForm documents: a fixed set of scalar fields, plus counts the
        # forms ask for explicitly.
        snapshot["participant_count"] = sum(len(p.meters) for p in participants)
        snapshot["installation_count"] = len(production)

        if doc_type == DOC_TYPE_ANNEX8_SWORN_DECLARATION:
            # Annex 8 is signed by ONE participant and the CRM cannot know
            # which, so the EANs are seeded only when the operation leaves no
            # choice and blank otherwise. A guessed EAN on a sworn declaration
            # is worse than a blank the reviewer fills. Seeding the keys at all
            # is what makes the fields appear in the review form.
            delivery = [meter.ean for p in participants for meter in p.meters]
            injection = [installation.ean for installation in production]
            snapshot["ean_delivery"] = delivery[0] if len(delivery) == 1 else None
            snapshot["ean_injection"] = injection[0] if len(injection) == 1 else None

    return snapshot


def merge_overrides(
    snapshot: Mapping[str, Any], overrides: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Apply the reviewer's corrections on top of the CRM-derived snapshot.

    Shallow by design: a list the reviewer submits *replaces* the CRM list rather
    than being merged row-by-row. Row-wise merging would silently resurrect a
    participant the reviewer deleted, which on a legal filing is the worse
    failure. Scalars are overwritten individually.
    """
    merged = copy.deepcopy(dict(snapshot))
    for key, value in (overrides or {}).items():
        merged[key] = copy.deepcopy(value)
    _restore_row_identity(snapshot, merged)
    return merged


def _restore_row_identity(derived: Mapping[str, Any], merged: dict[str, Any]) -> None:
    """Re-attach `ROW_MEMBER_ID` to rows the reviewer sent back without it.

    The generate dialog renders the derived rows and posts them back, so an
    override list is usually the same rows with some cells edited — but it
    arrives as plain JSON from a client that has no reason to echo an internal
    id. Without this, any correction to a sheet would strip the identity from
    every row on it and hide the whole filing from every member.

    Matching is by EAN where the block has one, and by the composed display name
    otherwise. A row that matches nothing keeps **no** id, and a row with no id
    is shown to nobody — a wrong attribution on a regulatory filing is far worse
    than an absent one.
    """
    for block in MEMBER_ROW_BLOCKS:
        merged_rows = merged.get(block)
        derived_rows = derived.get(block)
        if not isinstance(merged_rows, list) or not isinstance(derived_rows, list):
            continue
        by_ean: dict[Any, Any] = {}
        by_name: dict[Any, Any] = {}
        for row in derived_rows:
            if not isinstance(row, Mapping) or row.get(ROW_MEMBER_ID) is None:
                continue
            if row.get("ean") is not None:
                by_ean[row["ean"]] = row[ROW_MEMBER_ID]
            name_key = (row.get("nom"), row.get("prenom"), row.get("denomination"))
            if any(part is not None for part in name_key):
                # A homonym makes the name ambiguous, so drop it rather than
                # pick one: the fallback must not attribute a row to the wrong
                # member.
                by_name[name_key] = None if name_key in by_name else row[ROW_MEMBER_ID]
        for index, row in enumerate(merged_rows):
            if not isinstance(row, dict) or row.get(ROW_MEMBER_ID) is not None:
                continue
            resolved = by_ean.get(row.get("ean")) if row.get("ean") is not None else None
            if resolved is None:
                resolved = by_name.get((row.get("nom"), row.get("prenom"), row.get("denomination")))
            if resolved is not None:
                merged_rows[index] = {**row, ROW_MEMBER_ID: resolved}


# --------------------------------------------------------------------------- #
# Prefill warnings
#
# Raised here rather than in the adapter on purpose: the adapter's job is to
# describe the CRM, while "which blank matters for THIS filing" is a domain rule
# and this module is the only one that knows which keys each doc_type emits.
# Being session-free also keeps the rules unit-testable against plain dataclasses.
# --------------------------------------------------------------------------- #

#: Doc types whose payload includes a `members` sheet. Only these can be short of
#: a member field, so only these warn about one — otherwise the panel nags about
#: values the form never asks for and buries the warnings that matter.
_DOC_TYPES_WITH_MEMBERS = (DOC_TYPE_ANNEX6_NOTIFICATION, DOC_TYPE_ANNUAL_PARTICIPANT_LIST)

#: Community identity fields every CWaPE form opens with, as (snapshot key,
#: accessor). The key is the one `community_block` emits, so the frontend can
#: reuse its existing field labels rather than maintain a second vocabulary.
_COMMUNITY_REQUIRED: tuple[tuple[str, Callable[[CommunityContext], str | None]], ...] = (
    ("community_legal_name", lambda c: c.legal_name),
    ("community_vat_number", lambda c: c.vat_number),
    ("community_street", lambda c: c.address.street if c.address else None),
    ("community_number", lambda c: c.address.number if c.address else None),
    ("community_postcode", lambda c: c.address.postcode if c.address else None),
    ("community_city", lambda c: c.address.city if c.address else None),
)


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def ean_comb_digits(ean: object) -> str | None:
    """The 16 digits annex 8's comb boxes can hold, or None if they cannot.

    The CWaPE sworn declaration pre-prints "5 4" and then gives 16 boxes, so
    18 == 2 + 16 exactly and the value the form wants is the tail after the
    prefix. The regulator's own body text on that PDF says so: *"Le code EAN
    ... est compose de 18 chiffres"*.

    None rather than a truncation, always, for two reasons. The bundle manifest
    declares ``maxLength: 16``, so an 18-character value is a permanent docgen
    VALIDATION_ERROR and never renders anyway; and ``pdf_form`` spreads a value
    LEFT-aligned across the boxes, so a raw 18-digit string would drop the last
    two digits and print a WRONG EAN on a signed sworn declaration.
    """
    if not isinstance(ean, str):
        return None
    candidate = ean.strip()
    if not EAN_PATTERN.match(candidate) or not candidate.startswith(_EAN_FORM_PREFIX):
        return None
    return candidate[len(_EAN_FORM_PREFIX) :]


def project_comb_fields(doc_type: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Derive the comb-box keys from the semantic EAN keys, in place.

    Applied when the payload is FROZEN, not when it is prefilled: the review
    form derives its controls from the payload keys, so emitting the ``*_digits``
    keys at prefill time would show the reviewer four EAN fields, two of them a
    projection of the other two. Re-running it after ``merge_overrides`` (which
    is deliberately shallow) is also what stops an edited EAN leaving stale
    digits in the snapshot that gets filed.

    An unrepresentable value REMOVES the key rather than writing partial digits,
    so ``pdf_form._resolve_values`` skips it and the boxes stay exactly as the
    regulator printed them — still fillable by hand.
    """
    if doc_type != DOC_TYPE_ANNEX8_SWORN_DECLARATION:
        return snapshot
    for source, target in _ANNEX8_COMB_FIELDS:
        digits = ean_comb_digits(snapshot.get(source))
        if digits is None:
            snapshot.pop(target, None)
        else:
            snapshot[target] = digits
    return snapshot


def form_representation_warnings(
    doc_type: str, snapshot: Mapping[str, Any]
) -> tuple[PrefillWarning, ...]:
    """EANs this doc_type's form physically cannot print.

    Narrow like ``participant_warnings`` and for the same reason: only annex 8
    has the pre-printed prefix, so only annex 8 warns about it. A BLANK EAN
    produces no warning — the reviewer can see an empty field, and nagging
    about it would bury the warnings that matter.
    """
    if doc_type != DOC_TYPE_ANNEX8_SWORN_DECLARATION:
        return ()
    return tuple(
        PrefillWarning(
            code=WARNING_EAN_NOT_FORM_REPRESENTABLE,
            subject_type="meter",
            subject_id=str(snapshot[source]).strip(),
            params={"field": source},
        )
        for source, _target in _ANNEX8_COMB_FIELDS
        if isinstance(snapshot.get(source), str)
        and snapshot[source].strip()
        and ean_comb_digits(snapshot[source]) is None
    )


def community_warnings(community: CommunityContext) -> tuple[PrefillWarning, ...]:
    """Identity-header blanks the reviewer should fill in the CRM.

    ``community_legal_name`` is the one that earns this function. ``community_block``
    silently falls back to the DISPLAY name when there is no legal name, so the
    form fills in and *looks* correct while carrying the wrong legal identity onto
    a document filed with the regulator. Nothing else surfaces that.
    """
    return tuple(
        PrefillWarning(
            code=WARNING_COMMUNITY_FIELD_MISSING,
            subject_type="community",
            params={"field": key},
        )
        for key, accessor in _COMMUNITY_REQUIRED
        if _is_blank(accessor(community))
    )


def participant_warnings(
    doc_type: str, participants: Sequence[Participant]
) -> tuple[PrefillWarning, ...]:
    """Member fields the `members` sheet needs and the CRM does not have.

    Deliberately narrow. Address gaps are reported from `participant.address`
    only — `participant_rows` falls back to the meter's site address, which is
    the correct value for a delivery-point sheet and not a defect. And
    `numero_entreprise` is a company field: warning about it for an individual
    would be nagging about a cell the form leaves blank by design.
    """
    if doc_type not in _DOC_TYPES_WITH_MEMBERS:
        return ()

    warnings: list[PrefillWarning] = []
    for participant in participants:
        subject_id = str(participant.id_member)
        if participant.member_type == labels.MemberType.COMPANY and _is_blank(
            participant.vat_number
        ):
            warnings.append(
                PrefillWarning(
                    code=WARNING_MEMBER_FIELD_MISSING,
                    subject_type="member",
                    subject_id=subject_id,
                    params={"field": "numero_entreprise"},
                )
            )
        address = participant.address
        for key, value in (
            ("rue", address.street if address else None),
            ("code_postal", address.postcode if address else None),
            ("localite", address.city if address else None),
        ):
            if _is_blank(value):
                warnings.append(
                    PrefillWarning(
                        code=WARNING_MEMBER_FIELD_MISSING,
                        subject_type="member",
                        subject_id=subject_id,
                        params={"field": key},
                    )
                )
    return tuple(warnings)


def sort_warnings(warnings: Iterable[PrefillWarning]) -> list[PrefillWarning]:
    """Deterministic order, so the panel does not reshuffle between prefills."""
    return sorted(
        warnings,
        key=lambda w: (w.subject_type, w.subject_id or "", w.code, w.params.get("field", "")),
    )


def rows_for_members(
    snapshot: Mapping[str, Any] | None, member_ids: Iterable[int]
) -> dict[str, list[dict[str, Any]]]:
    """The rows of `snapshot` belonging to `member_ids`, per block.

    The whole of decision B4's filtering, kept pure so it can be tested against
    literal snapshots rather than through a request.

    Fail-closed in three ways, each deliberate:

    * a row whose `ROW_MEMBER_ID` is absent or None matches nobody — a filing
      frozen before the key existed shows as "nothing on record", never as a
      guess based on a name;
    * a value that is not an int (a reviewer can post arbitrary JSON) is
      compared after an explicit int() that swallows nothing — anything
      unparseable is simply not a match;
    * blocks are enumerated from `MEMBER_ROW_BLOCKS`, so a new sheet added to a
      snapshot is invisible here until it is listed, rather than being copied
      out wholesale by a generic walk.
    """
    wanted = {int(member_id) for member_id in member_ids}
    result: dict[str, list[dict[str, Any]]] = {block: [] for block in MEMBER_ROW_BLOCKS}
    if not isinstance(snapshot, Mapping) or not wanted:
        return result

    for block in MEMBER_ROW_BLOCKS:
        rows = snapshot.get(block)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            raw = row.get(ROW_MEMBER_ID)
            if raw is None:
                continue
            try:
                owner = int(raw)
            except (TypeError, ValueError):
                continue
            if owner in wanted:
                result[block].append(dict(row))
    return result
