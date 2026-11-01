"""
Read-only property endpoints.

Indexed lookups only, key-protected and never proxied to the browser. The
models are bound to the literal ``production`` schema, whatever ``DB_SCHEMA``
the process exports.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.deps import get_session
from hpldn.backend.api.schemas import (
    EpcSummary,
    PropertyDetail,
    PropertySummary,
    TransactionRead,
)
from hpldn.backend.models.production import EPC, PPD, Address, UprnLookup

router = APIRouter(prefix="/properties", tags=["properties"])

MAX_TRANSACTION_HISTORY = 500

_address_with_uprn = select(Address, UprnLookup.uprn).join(
    UprnLookup, UprnLookup.address_id == Address.id
)


def _summary(address: Address, uprn: int) -> PropertySummary:
    """Build a `PropertySummary` from an address row."""
    return PropertySummary(
        uprn=uprn,
        postnumber=address.postnumber,
        poststreet=address.poststreet,
        posttown=address.posttown,
        postcode=address.postcode,
        postcounty=address.postcounty,
        unit=address.unit,
    )


async def _get_address_row(session: AsyncSession, uprn: int) -> Row:
    """
    Return the address row for `uprn`.

    Raises
    ------
    HTTPException
        404 if no address has that UPRN.
    """
    row = (
        await session.execute(_address_with_uprn.where(UprnLookup.uprn == uprn))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No property with UPRN {uprn}")
    return row


@router.get("", response_model=list[PropertySummary])
async def list_properties(
    postcode: str | None = Query(None, description="Exact postcode, e.g. SW1A 1AA"),
    posttown: str | None = Query(None, description="Post town, case-insensitive"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[PropertySummary]:
    """List addresses, optionally filtered by exact postcode or post town."""
    stmt = _address_with_uprn
    if postcode:
        stmt = stmt.where(Address.postcode == postcode.strip().upper())
    if posttown:
        stmt = stmt.where(Address.posttown.ilike(posttown.strip()))
    stmt = stmt.order_by(Address.id).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).all()
    return [_summary(row.Address, row.uprn) for row in rows]


@router.get("/{uprn}", response_model=PropertyDetail)
async def get_property(
    uprn: int, session: AsyncSession = Depends(get_session)
) -> PropertyDetail:
    """Return one property's address, latest EPC and transaction history."""
    row = await _get_address_row(session, uprn)
    address = row.Address
    epc = (
        (
            await session.execute(
                select(EPC)
                .where(EPC.address_id == address.id)
                .order_by(EPC.lodgement_datetime.desc().nulls_last())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    transactions = (
        (
            await session.execute(
                select(PPD)
                .where(PPD.address_id == address.id)
                .order_by(PPD.dateoftransfer.desc())
                .limit(MAX_TRANSACTION_HISTORY)
            )
        )
        .scalars()
        .all()
    )
    return PropertyDetail(
        **_summary(address, row.uprn).model_dump(),
        epc=EpcSummary.model_validate(epc) if epc is not None else None,
        transactions=[TransactionRead.model_validate(t) for t in transactions],
    )


@router.get("/{uprn}/transactions", response_model=list[TransactionRead])
async def get_property_transactions(
    uprn: int, session: AsyncSession = Depends(get_session)
) -> list[TransactionRead]:
    """Return one property's transactions, newest first."""
    row = await _get_address_row(session, uprn)
    transactions = (
        (
            await session.execute(
                select(PPD)
                .where(PPD.address_id == row.Address.id)
                .order_by(PPD.dateoftransfer.desc())
                .limit(MAX_TRANSACTION_HISTORY)
            )
        )
        .scalars()
        .all()
    )
    return [TransactionRead.model_validate(t) for t in transactions]
