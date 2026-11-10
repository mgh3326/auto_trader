import pytest

from app.services.account_value.broker_views import (
    BrokerMethodRefusedError,
    read_only_view,
)


class OrderCapable:
    def fetch_my_stocks(self):
        return ["holding"]

    def place_order(self):
        raise AssertionError("must not be reached")


def test_only_allowlisted_methods_are_exposed():
    view = read_only_view("kis", OrderCapable())
    assert view.fetch_my_stocks() == ["holding"]
    with pytest.raises(BrokerMethodRefusedError):
        view.place_order()
    with pytest.raises(BrokerMethodRefusedError):
        view.target = OrderCapable()
