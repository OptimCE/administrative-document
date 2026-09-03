"""The single place coupled to the CRM core table layout. Every query is SELECT-only.

Nothing in this service writes to the CRM database. Keeping the raw SQL here (as
opposed to mapping whole CRM tables as ORM models) means a CRM schema change
breaks one file with an obvious diff, rather than leaking through the codebase.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from domain.regions import region_for_regulator
from ports.crm_core import (
    WARNING_METER_NO_MEMBER,
    CommunityContext,
    Installation,
    MeterPoint,
    OperationParticipants,
    Participant,
    PostalAddress,
    PrefillWarning,
    SharingOperation,
)

# meter_data.status value for an active attribution (MeterDataStatus.ACTIVE).
_ACTIVE_METER_DATA_STATUS = 1

_COMMUNITY_IDENTITY_SQL = text(
    """
    SELECT c.id,
           c.name,
           c.regulator,
           c.legal_name,
           c.vat_number,
           a.street,
           a.number     AS address_number,
           a.postcode,
           a.city,
           a.supplement
      FROM community c
      LEFT JOIN address a ON a.id = c.headquarters_address_id
     WHERE c.id = :id_community
    """
)


# Both operation queries filter on id_community: a dossier may only ever point at
# an operation of its own community.
_SHARING_OPERATION_SQL = text(
    """
    SELECT id, name, id_community
      FROM sharing_operation
     WHERE id = :id_sharing_operation
       AND id_community = :id_community
    """
)

_SHARING_OPERATIONS_SQL = text(
    """
    SELECT id, name, id_community
      FROM sharing_operation
     WHERE id_community = :id_community
     ORDER BY name ASC, id ASC
    """
)


# One row per ACTIVE meter attribution in the operation, carrying the member's
# identity and both the meter's own address and the member's home address. The
# annexes list a delivery point at the meter's address, so that one wins when
# present. member is LEFT JOINed: an attributed-to-nobody meter must still show
# up (as an orphan warning), not vanish.
_OPERATION_METERS_SQL = text(
    """
    SELECT md.ean                       AS ean,
           md.id_member                 AS id_member,
           md.client_type               AS client_type,
           md.injection_status          AS injection_status,
           md.production_chain          AS production_chain,
           md.total_generating_capacity AS generating_capacity,
           md.grd                       AS grd,
           md.start_date                AS start_date,
           m.meter_number               AS meter_number,
           mem.name                     AS member_name,
           mem.member_type              AS member_type,
           i.first_name                 AS first_name,
           i.email                      AS individual_email,
           co.vat_number                AS vat_number,
           mgr.email                    AS manager_email,
           -- "site_" and not "meter_": meter_number is the meter's own number,
           -- and a `meter_number` address alias would collide with it.
           ma.street                    AS site_street,
           ma.number                    AS site_number,
           ma.postcode                  AS site_postcode,
           ma.city                      AS site_city,
           ma.supplement                AS site_supplement,
           ha.street                    AS home_street,
           ha.number                    AS home_number,
           ha.postcode                  AS home_postcode,
           ha.city                      AS home_city,
           ha.supplement                AS home_supplement
      FROM meter_data md
      JOIN meter       m   ON m.ean = md.ean
      LEFT JOIN member mem ON mem.id = md.id_member
      LEFT JOIN individual i  ON i.id = mem.id
      LEFT JOIN company    co ON co.id = mem.id
      LEFT JOIN manager   mgr ON mgr.id = COALESCE(co.id_manager, i.id_manager)
      LEFT JOIN address   ma  ON ma.id = m.id_address
      LEFT JOIN address   ha  ON ha.id = mem.id_home_address
     WHERE md.id_community = :id_community
       AND md.id_sharing_operation = :id_sharing_operation
       AND md.status = :active
     ORDER BY mem.name NULLS LAST, mem.id, md.ean
    """
)


def _as_optional_str(value: object) -> str | None:
    """Normalise a CRM column to a string.

    ``address.number`` was an INTEGER in the CRM until 2026-08-30, when it became
    a VARCHAR(32); a naive join into an address line raised TypeError. The
    conversion is now usually a no-op, and is kept deliberately: it is what lets
    this adapter run against a CRM on either side of that migration.
    """
    if value is None:
        return None
    return str(value)


def _address(row: dict[str, Any], prefix: str) -> PostalAddress | None:
    """Build an address from ``<prefix>_*`` columns, or None if all are NULL."""
    columns = {
        "street": f"{prefix}_street",
        "number": f"{prefix}_number",
        "postcode": f"{prefix}_postcode",
        "city": f"{prefix}_city",
        "supplement": f"{prefix}_supplement",
    }
    values = {field: row.get(column) for field, column in columns.items()}
    if not any(value is not None for value in values.values()):
        return None
    # Coerce at the boundary so no downstream formatter has to care. Getting this
    # wrong silently broke billing's whole issue pipeline once — which is why the
    # coercion stays even though address.number is text since 2026-08-30.
    return PostalAddress(**{field: _as_optional_str(value) for field, value in values.items()})


def _installation(row: dict[str, Any]) -> Installation:
    return Installation(
        ean=row["ean"],
        meter_number=_as_optional_str(row["meter_number"]),
        grd=_as_optional_str(row["grd"]),
        id_member=row["id_member"],
        member_name=row["member_name"],
        member_first_name=row["first_name"],
        member_type=row["member_type"],
        injection_status=row["injection_status"],
        production_chain=row["production_chain"],
        generating_capacity=row["generating_capacity"],
        commissioned_on=row["start_date"],
        address=_address(row, "site") or _address(row, "home"),
    )


def _participant(id_member: int, rows: list[dict[str, Any]]) -> Participant:
    first = rows[0]
    return Participant(
        id_member=id_member,
        name=first["member_name"],
        member_type=first["member_type"],
        first_name=first["first_name"],
        vat_number=_as_optional_str(first["vat_number"]),
        email=first["individual_email"] or first["manager_email"],
        address=_address(first, "home"),
        meters=tuple(
            MeterPoint(
                ean=row["ean"],
                meter_number=_as_optional_str(row["meter_number"]),
                grd=_as_optional_str(row["grd"]),
                client_type=row["client_type"],
                injection_status=row["injection_status"],
                address=_address(row, "site") or _address(row, "home"),
            )
            for row in rows
        ),
    )


class SqlAlchemyCrmCoreRead:
    """CrmCoreReadPort backed by the read-only CRM engine."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_community_context(self, id_community: int) -> CommunityContext | None:
        result = await self._session.execute(
            _COMMUNITY_IDENTITY_SQL, {"id_community": id_community}
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        has_address = any(
            row[column] is not None
            for column in ("street", "address_number", "postcode", "city", "supplement")
        )
        address = (
            PostalAddress(
                street=_as_optional_str(row["street"]),
                number=_as_optional_str(row["address_number"]),
                postcode=_as_optional_str(row["postcode"]),
                city=_as_optional_str(row["city"]),
                supplement=_as_optional_str(row["supplement"]),
            )
            if has_address
            else None
        )

        return CommunityContext(
            id_community=row["id"],
            name=row["name"],
            regulator=row["regulator"],
            region=region_for_regulator(row["regulator"]),
            legal_name=row["legal_name"],
            vat_number=row["vat_number"],
            address=address,
        )

    async def get_sharing_operation(
        self, id_community: int, id_sharing_operation: int
    ) -> SharingOperation | None:
        result = await self._session.execute(
            _SHARING_OPERATION_SQL,
            {"id_community": id_community, "id_sharing_operation": id_sharing_operation},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return SharingOperation(id=row["id"], name=row["name"], id_community=row["id_community"])

    async def list_sharing_operations(self, id_community: int) -> list[SharingOperation]:
        result = await self._session.execute(
            _SHARING_OPERATIONS_SQL, {"id_community": id_community}
        )
        return [
            SharingOperation(id=row["id"], name=row["name"], id_community=row["id_community"])
            for row in result.mappings().all()
        ]

    async def get_operation_participants(
        self, id_community: int, id_sharing_operation: int
    ) -> OperationParticipants:
        """Fold the operation's active meter attributions into the annexe lists.

        Note on storage: the CRM does not model storage units — ``meter_data``
        distinguishes only consumption from injection. The sharing form's storage
        sheet is therefore returned empty and filled by hand in the review step,
        rather than being guessed at from a production chain.
        """
        result = await self._session.execute(
            _OPERATION_METERS_SQL,
            {
                "id_community": id_community,
                "id_sharing_operation": id_sharing_operation,
                "active": _ACTIVE_METER_DATA_STATUS,
            },
        )
        rows: list[dict[str, Any]] = [dict(row) for row in result.mappings().all()]

        by_member: dict[int, list[dict[str, Any]]] = {}
        production: list[Installation] = []
        warnings: list[PrefillWarning] = []

        for row in rows:
            if row["injection_status"] is not None:
                production.append(_installation(row))
            id_member = row["id_member"]
            if id_member is None:
                # A delivery point nobody owns cannot be listed under a name;
                # surface it rather than silently dropping it from a legal form.
                # The EAN identifies the meter, so the reviewer can open it and
                # attach a holder instead of typing a name over the gap.
                warnings.append(
                    PrefillWarning(
                        code=WARNING_METER_NO_MEMBER,
                        subject_type="meter",
                        subject_id=str(row["ean"]),
                    )
                )
                continue
            by_member.setdefault(id_member, []).append(row)

        participants = tuple(
            _participant(id_member, member_rows) for id_member, member_rows in by_member.items()
        )
        return OperationParticipants(
            participants=participants,
            production=tuple(production),
            storage=(),
            warnings=tuple(warnings),
        )

    async def member_ids_for_user(self, *, id_community: int, auth_user_id: str) -> list[int]:
        """The member id(s) the authenticated user represents in this community."""
        result = await self._session.execute(
            text(
                """
                SELECT DISTINCT m.id AS id
                FROM member m
                JOIN user_member_link uml ON uml.id_member = m.id
                JOIN app_user au ON au.id = uml.id_user
                WHERE m.id_community = :cid AND au.auth_user_id = :auth_user_id
                ORDER BY m.id
                """
            ),
            {"cid": id_community, "auth_user_id": auth_user_id},
        )
        return [int(row["id"]) for row in result.mappings()]
