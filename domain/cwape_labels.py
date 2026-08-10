#
# ruff: noqa: RUF001
#     Every string below is transcribed character-for-character from a CWaPE
#     workbook's data-validation list. That includes the typographic apostrophe
#     (U+2019) French uses — "normalising" it to an ASCII quote would make the
#     value stop matching the dropdown, which is the one thing this file exists
#     to prevent.
"""CRM coded values → the exact French labels the CWaPE forms accept.

The mandated workbooks are not free text: most columns carry an Excel
data-validation list, so a value that is not character-for-character one of the
allowed entries shows up as a validation error the moment the regulator opens the
file. These tables are therefore a contract with the *form*, not a display
concern, and `tests/domain/test_cwape_labels.py` re-reads the validations out of
the shipped templates to prove they still agree.

Anything the CRM cannot determine returns ``None`` rather than a guess: the
review step in the UI is where a human supplies it. A wrong-but-plausible label
on a filed regulatory document is worse than a blank the user is asked to fill.

Pure module — no I/O, no session — so every mapping is unit-testable directly.
"""

from __future__ import annotations

from enum import IntEnum

# ---------------------------------------------------------------------------
# CRM coded values (crm-backend/src/modules/{members,meters}/shared/*.types.ts).
# Mirrored rather than imported: the CRM is a separate service in TypeScript.
# ---------------------------------------------------------------------------


class MemberType(IntEnum):
    INDIVIDUAL = 1
    COMPANY = 2


class ClientType(IntEnum):
    RESIDENTIAL = 1
    PROFESSIONAL = 2
    INDUSTRIAL = 3


class InjectionStatus(IntEnum):
    AUTOPROD_OWNER = 1
    AUTOPROD_RIGHTS = 2
    INJECTION_OWNER = 3
    INJECTION_RIGHTS = 4


class ProductionChain(IntEnum):
    PHOTOVOLTAIC = 1
    WIND = 2
    HYDRO = 3
    BIOMASS = 4
    BIOGAS = 5
    COGEN_FOSSIL = 6
    OTHER = 7


# ---------------------------------------------------------------------------
# The form vocabularies, verbatim.
# ---------------------------------------------------------------------------

#: "Gestionnaire de Réseau" — the only DSOs the forms accept. The CRM stores
#: ``meter_data.grd`` as free text, so it is matched case-insensitively and an
#: unrecognised operator becomes None (the reviewer picks one).
GRD_LABELS: tuple[str, ...] = ("ELIA", "AIEG", "AIESH", "ORES", "RESA", "REW")

#: "Client résidentiel"
YES, NO = "OUI", "NON"

#: "Statut de l'installation". The CRM has no commissioning flag, so this is
#: never derived — it is a review-form field.
INSTALLATION_IN_SERVICE = "EN SERVICE"
INSTALLATION_TO_INSTALL = "A INSTALLER"

#: "Statut du producteur", as used on the *sharing* form (annex 6 / 5611).
PRODUCER_STATUS_LABELS: dict[int, str] = {
    InjectionStatus.AUTOPROD_OWNER: "Autoproducteur - propriétaire",
    InjectionStatus.AUTOPROD_RIGHTS: "Autoproducteur - droit de jouissance",
    InjectionStatus.INJECTION_OWNER: "Injection pure - propriétaire",
    InjectionStatus.INJECTION_RIGHTS: "Injection pure - droit de jouissance",
}

#: "Statut du producteur" on the *notification* form (annex 6 / 5617), whose
#: production sheet validates rows 4-53 against the workbook's `Statut` named
#: range — a different, community-centric vocabulary from the sharing form's.
#: Ownership vs usage right is the only distinction the CRM can make.
COMMUNITY_INSTALLATION_STATUS_LABELS: dict[int, str] = {
    InjectionStatus.AUTOPROD_OWNER: "CE propriétaire de l'installation",
    InjectionStatus.INJECTION_OWNER: "CE propriétaire de l'installation",
    InjectionStatus.AUTOPROD_RIGHTS: "CE titulaire d'un droit de jouissance sur l'installation",
    InjectionStatus.INJECTION_RIGHTS: "CE titulaire d'un droit de jouissance sur l'installation",
}

#: "Filière de production". ``OTHER`` is deliberately absent: the forms offer no
#: catch-all, so the reviewer must choose a real one.
PRODUCTION_CHAIN_LABELS: dict[int, str] = {
    ProductionChain.PHOTOVOLTAIC: "Photovoltaïque",
    ProductionChain.WIND: "Eolien",
    ProductionChain.HYDRO: "Hydro-électricité",
    ProductionChain.BIOMASS: "Biomasse solide",
    ProductionChain.BIOGAS: "Biogaz",
    ProductionChain.COGEN_FOSSIL: "Cogénération fossile",
}

#: "Catégorie" on the notification form's member sheet. Only two of the four
#: entries are derivable from ``member_type``; a public authority or an "Autre"
#: is a legal classification the CRM does not record.
MEMBER_CATEGORY_LABELS: dict[int, str] = {
    MemberType.INDIVIDUAL: "Personne physique",
    MemberType.COMPANY: "Entreprise",
}

#: Every value the "Catégorie" dropdown offers, in workbook order — the review
#: form presents these so a reviewer can override the derived default.
MEMBER_CATEGORY_CHOICES: tuple[str, ...] = (
    "Personne physique",
    "Autorité locale située en Région wallonne",
    "Entreprise",
    "Autre",
)

#: "Sous-catégorie", cascading from the category (the workbook wires this with
#: INDIRECT(LEFT(A4,8)), so the sub-list name is the category's first 8 chars).
#: Not derivable from the CRM; offered to the reviewer.
MEMBER_SUBCATEGORY_CHOICES: dict[str, tuple[str, ...]] = {
    "Personne physique": ("N/A",),
    "Autre": ("N/A",),
    "Entreprise": ("Petite entreprise", "Moyenne entreprise", "Grande entreprise"),
    "Autorité locale située en Région wallonne": (
        "1° Commune ou Province wallonne",
        "2° Intercommunale ou intercommunale interrégionale",
        "3° Association de projet",
        "4° Association de pouvoirs publics visées à l’article 118 de la loi organique "
        "du 8 juillet 1976 des centres publics d’action sociale",
        "5° Société de logement de service public",
        "6° Zone de police",
        "7° Zone de secours",
        "8° Centre Public d’Action Sociale",
        "9° Etablissement chargé de la gestion du temporel des cultes reconnus, fabrique "
        "d’églises ou établissement chargé de la gestion des intérêts de la communauté "
        "philosophique non confessionnelle",
        "10° Régie provinciale ou communale autonome",
        "11° A.S.B.L. locale visée à l’article L5111-1, 18° du code de la démocratie "
        "locale et de la décentralisation",
        "12° Tout établissement scolaire visé aux points 10° à 13° de l’article 4, "
        "alinéa 1er de l’AGW communautés et partage",
        "13° Toute personne morale contrôlée par les entités visées aux points 1° à 16° "
        "de l’AGW communautés et partage (entité mixte)",
    ),
}


# ---------------------------------------------------------------------------
# Mapping functions. All total: an unknown code yields None, never an exception
# and never a fabricated label.
# ---------------------------------------------------------------------------


def grd_label(grd: str | None) -> str | None:
    """Normalise ``meter_data.grd`` (free text) to an accepted DSO name."""
    if not grd:
        return None
    candidate = grd.strip().upper()
    return candidate if candidate in GRD_LABELS else None


def residential_label(client_type: int | None) -> str | None:
    """ "Client résidentiel" — OUI only for the residential segment."""
    if client_type is None:
        return None
    return YES if client_type == ClientType.RESIDENTIAL else NO


def producer_status_label(injection_status: int | None) -> str | None:
    """ "Statut du producteur" for the sharing form."""
    if injection_status is None:
        return None
    return PRODUCER_STATUS_LABELS.get(injection_status)


def community_installation_status_label(injection_status: int | None) -> str | None:
    """ "Statut du producteur" for the community-notification form."""
    if injection_status is None:
        return None
    return COMMUNITY_INSTALLATION_STATUS_LABELS.get(injection_status)


def production_chain_label(production_chain: int | None) -> str | None:
    """ "Filière de production"."""
    if production_chain is None:
        return None
    return PRODUCTION_CHAIN_LABELS.get(production_chain)


def member_category_label(member_type: int | None) -> str | None:
    """ "Catégorie" — the derivable default; a reviewer may pick another."""
    if member_type is None:
        return None
    return MEMBER_CATEGORY_LABELS.get(member_type)


def display_name(name: str | None, first_name: str | None, member_type: int | None) -> str:
    """ "Nom et prénom ou dénomination" — one column for both member kinds.

    ``member.name`` is the surname for an individual and the legal name for a
    company, so only the former takes a first name.
    """
    if member_type == MemberType.INDIVIDUAL and first_name:
        return f"{first_name} {name or ''}".strip()
    return (name or "").strip()
