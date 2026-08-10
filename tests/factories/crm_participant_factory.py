"""Insert the CRM rows the Phase-2 pre-fill reads.

Raw SQL, like ``conftest.create_sharing_operation``: this service never maps CRM
tables as ORM models — it reads them only through
``ports/crm_core_sqlalchemy.py`` — so there is nothing to reuse.
"""

from __future__ import annotations

import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_address(
    session: AsyncSession,
    *,
    street: str = "Rue Haute",
    number: int = 12,
    postcode: str = "5000",
    city: str = "Namur",
    supplement: str | None = None,
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO address (street, number, postcode, city, supplement) "
            "VALUES (:street, :number, :postcode, :city, :supplement) RETURNING id"
        ),
        {
            "street": street,
            "number": number,
            "postcode": postcode,
            "city": city,
            "supplement": supplement,
        },
    )
    await session.flush()
    return int(result.scalar_one())


async def create_member(
    session: AsyncSession,
    *,
    id_community: int,
    name: str,
    member_type: int = 1,
    first_name: str | None = None,
    email: str | None = None,
    vat_number: str | None = None,
    id_home_address: int | None = None,
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO member (name, member_type, status, id_community, id_home_address) "
            "VALUES (:name, :member_type, 1, :id_community, :id_home_address) RETURNING id"
        ),
        {
            "name": name,
            "member_type": member_type,
            "id_community": id_community,
            "id_home_address": id_home_address,
        },
    )
    id_member = int(result.scalar_one())
    if member_type == 2:
        await session.execute(
            text("INSERT INTO company (id, vat_number) VALUES (:id, :vat)"),
            {"id": id_member, "vat": vat_number},
        )
    else:
        await session.execute(
            text(
                "INSERT INTO individual (id, first_name, email, social_rate) "
                "VALUES (:id, :first_name, :email, FALSE)"
            ),
            {"id": id_member, "first_name": first_name, "email": email},
        )
    await session.flush()
    return id_member


async def create_meter(
    session: AsyncSession,
    *,
    ean: str,
    id_community: int,
    meter_number: str | None = None,
    id_address: int | None = None,
) -> str:
    await session.execute(
        text(
            "INSERT INTO meter (ean, meter_number, id_address, id_community) "
            "VALUES (:ean, :meter_number, :id_address, :id_community)"
        ),
        {
            "ean": ean,
            "meter_number": meter_number,
            "id_address": id_address,
            "id_community": id_community,
        },
    )
    await session.flush()
    return ean


async def attribute_meter(
    session: AsyncSession,
    *,
    ean: str,
    id_community: int,
    id_sharing_operation: int,
    id_member: int | None = None,
    status: int = 1,
    client_type: int | None = 1,
    injection_status: int | None = None,
    production_chain: int | None = None,
    generating_capacity: float | None = None,
    grd: str | None = "ORES",
    start_date: datetime.date | None = None,
) -> None:
    """Insert a ``meter_data`` window — the CRM's meter-to-member attribution."""
    await session.execute(
        text(
            "INSERT INTO meter_data "
            "(ean, id_member, id_sharing_operation, status, client_type, injection_status, "
            " production_chain, total_generating_capacity, grd, start_date, id_community) "
            "VALUES (:ean, :id_member, :op, :status, :client_type, :injection_status, "
            " :production_chain, :capacity, :grd, :start_date, :id_community)"
        ),
        {
            "ean": ean,
            "id_member": id_member,
            "op": id_sharing_operation,
            "status": status,
            "client_type": client_type,
            "injection_status": injection_status,
            "production_chain": production_chain,
            "capacity": generating_capacity,
            "grd": grd,
            "start_date": start_date or datetime.date(2026, 1, 1),
            "id_community": id_community,
        },
    )
    await session.flush()


async def create_app_user(
    session: AsyncSession, *, auth_user_id: str, email: str | None = None
) -> int:
    """A CRM app_user (the Keycloak identity); returns its id for user_member_link."""
    result = await session.execute(
        text("INSERT INTO app_user (auth_user_id, email) VALUES (:auth, :email) RETURNING id"),
        {"auth": auth_user_id, "email": email or f"{auth_user_id}@example.be"},
    )
    return int(result.scalar_one())


async def link_user_to_member(session: AsyncSession, *, id_user: int, id_member: int) -> None:
    """Make `id_user` a representative of `id_member`.

    The link carries no community: scope comes from `member.id_community`, which
    is why `member_ids_for_user` joins the member rather than this table alone.
    """
    await session.execute(
        text("INSERT INTO user_member_link (id_user, id_member) VALUES (:u, :m)"),
        {"u": id_user, "m": id_member},
    )
