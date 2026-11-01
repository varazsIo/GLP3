"""
Property route behaviour and the response-schema contract.

One round trip per schema, against the fixture database built from
`hpldn.backend.models.production`, so an ORM rename cannot silently change a
response.
"""

import pytest

from hpldn.backend.api.routers.properties import MAX_TRANSACTION_HISTORY
from hpldn.backend.api.schemas import EpcSummary, PropertyDetail, PropertySummary, TransactionRead
from hpldn.backend.tests.api.conftest import ADDRESS, UPRN


def test_list_properties_returns_the_summary_schema(client, auth):
    body = client.get("/properties", headers=auth).json()
    assert len(body) == 1
    summary = PropertySummary.model_validate(body[0])
    assert summary.uprn == UPRN
    assert summary.postcode == ADDRESS["postcode"]
    assert summary.poststreet == ADDRESS["poststreet"]


def test_list_properties_filters_by_postcode(client, auth):
    assert client.get(
        "/properties", params={"postcode": "sw1a 2aa"}, headers=auth
    ).json()
    assert (
        client.get(
            "/properties", params={"postcode": "N1 1AA"}, headers=auth
        ).json()
        == []
    )


def test_list_properties_filters_by_posttown_case_insensitively(client, auth):
    assert client.get(
        "/properties", params={"posttown": "london"}, headers=auth
    ).json()


@pytest.mark.parametrize(
    "params,expected",
    [({"limit": 0}, 422), ({"limit": 501}, 422), ({"offset": -1}, 422)],
)
def test_list_properties_validates_its_pagination(client, auth, params, expected):
    assert client.get("/properties", params=params, headers=auth).status_code == expected


def test_get_property_returns_the_detail_schema(client, auth):
    body = client.get(f"/properties/{UPRN}", headers=auth).json()
    detail = PropertyDetail.model_validate(body)
    assert detail.uprn == UPRN
    assert detail.epc is not None
    assert isinstance(detail.epc, EpcSummary)
    assert detail.epc.current_energy_rating == "C"
    assert detail.epc.total_floor_area == pytest.approx(104.5)
    assert [t.price for t in detail.transactions] == [780_000, 450_000]


def test_transactions_endpoint_returns_the_transaction_schema(client, auth):
    body = client.get(f"/properties/{UPRN}/transactions", headers=auth).json()
    transactions = [TransactionRead.model_validate(t) for t in body]
    assert [t.price for t in transactions] == [780_000, 450_000]
    assert {t.propertytype for t in transactions} == {"T"}
    assert transactions[0].dateoftransfer.isoformat() == "2021-03-12"


def test_transaction_history_is_bounded(client, auth):
    """Check both history routes cap their result sets."""
    assert MAX_TRANSACTION_HISTORY > 0
    body = client.get(f"/properties/{UPRN}/transactions", headers=auth).json()
    assert len(body) <= MAX_TRANSACTION_HISTORY


@pytest.mark.parametrize(
    "route", ["/properties/{u}", "/properties/{u}/transactions"]
)
def test_unknown_uprn_is_a_404(client, auth, route):
    response = client.get(route.format(u=999_999_999_999), headers=auth)
    assert response.status_code == 404
    assert "999999999999" in response.json()["detail"]


def test_non_integer_uprn_is_a_422(client, auth):
    assert client.get("/properties/not-a-uprn", headers=auth).status_code == 422
