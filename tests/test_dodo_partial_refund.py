"""Partial refunds through Dodo Payments (Refund Policy v3.0 §3.3).

The dodopayments SDK (>=1.118) refunds part of a payment per line item:
refunds.create(payment_id, items=[{item_id, amount, tax_inclusive}]), and
payments.retrieve_line_items says what is still refundable. Runs the real
services/refunds.py; only the Dodo client is stubbed.
"""
import asyncio
import inspect
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import PaymentOrder
from services import dodo_payments, refunds


class _Client:
    def __init__(self, currency="USD", items=None):
        self.calls = []
        self._lines = SimpleNamespace(currency=currency, items=items if items is not None else [
            SimpleNamespace(items_id="pdt_outreach", amount=2700, refundable_amount=2700, tax=0)])
        client = self

        class _Payments:
            async def retrieve_line_items(self, payment_id):
                client.calls.append(("line_items", payment_id))
                return client._lines

        class _Refunds:
            async def create(self, **kw):
                client.calls.append(("refund", kw))
                return SimpleNamespace(refund_id="ref_1")

        self.payments, self.refunds = _Payments(), _Refunds()


def _order(**kw):
    base = dict(id=9, user_id="u", provider="dodo", dodo_payment_id="pay_1", amount_cents=2700, currency="USD",
                tier=350, status="paid", refunded_cents=None)
    base.update(kw)
    return PaymentOrder(**base)


@pytest.fixture()
def client(monkeypatch):
    c = _Client()
    monkeypatch.setattr(dodo_payments, "_get_client", lambda: c)
    return c


def test_partial_refund_goes_to_the_line_item(client):
    rid = asyncio.run(refunds._provider_refund(_order(), "§3.3 unsent credits", amount_cents=1100))
    assert rid == "ref_1"
    assert client.calls == [
        ("line_items", "pay_1"),
        ("refund", {"payment_id": "pay_1", "reason": "§3.3 unsent credits",
                    "items": [{"item_id": "pdt_outreach", "amount": 1100, "tax_inclusive": True}]}),
    ]


def test_remainder_after_a_partial_is_refunded_by_item(client):
    asyncio.run(refunds._provider_refund(_order(refunded_cents=1100), "rest"))
    assert client.calls[-1][1]["items"] == [{"item_id": "pdt_outreach", "amount": 1600, "tax_inclusive": True}]


def test_full_refund_is_unchanged(client):
    asyncio.run(refunds._provider_refund(_order(), "full"))
    assert client.calls == [("refund", {"payment_id": "pay_1", "reason": "full"})]


@pytest.mark.parametrize("c,msg", [
    (_Client(currency="INR"), "charged this payment in INR"),
    (_Client(items=[]), "0 refundable line items"),
    (_Client(items=[SimpleNamespace(items_id="a", refundable_amount=500), SimpleNamespace(items_id="b",
                                                                                          refundable_amount=500)]),
     "2 refundable line items"),
    (_Client(items=[SimpleNamespace(items_id="a", refundable_amount=900)]), "at most 900"),
])
def test_partial_refund_refuses_what_it_cannot_do_exactly(monkeypatch, c, msg):
    monkeypatch.setattr(dodo_payments, "_get_client", lambda: c)
    with pytest.raises(refunds.RefundError, match=msg):
        asyncio.run(refunds._provider_refund(_order(), "x", amount_cents=1100))
    assert not [k for k, _ in c.calls if k == "refund"]


def test_installed_sdk_has_the_call_shape_we_use():
    from dodopayments.resources.payments import AsyncPaymentsResource
    from dodopayments.resources.refunds import AsyncRefundsResource
    from dodopayments.types.refund_create_params import Item

    assert "items" in inspect.signature(AsyncRefundsResource.create).parameters
    assert {"item_id", "amount", "tax_inclusive"} <= set(Item.__annotations__)
    assert hasattr(AsyncPaymentsResource, "retrieve_line_items")
