import unittest
from unittest.mock import patch

from bybit_adapter import BybitDemoClient, BybitError, normalize_position


class BybitAdapterTests(unittest.TestCase):
    def test_only_demo_host_is_allowed(self):
        for host in ("https://api.bybit.com", "https://api-testnet.bybit.com"):
            with self.assertRaises(BybitError):
                BybitDemoClient("key", "secret", host)

    def test_position_is_normalized(self):
        position = normalize_position({
            "symbol": "ETHUSDT", "side": "Sell", "size": "2",
            "avgPrice": "3000", "markPrice": "2990", "liqPrice": "3300",
            "leverage": "10", "positionIdx": 0,
        })
        self.assertEqual(position["instrument"], "ETHUSDT")
        self.assertEqual(position["side"], "short")
        self.assertEqual(position["size"], 2.0)

    def test_stop_validation_is_fail_closed(self):
        client = BybitDemoClient("key", "secret")
        short = normalize_position({
            "symbol": "ETHUSDT", "side": "Sell", "size": "1",
            "avgPrice": "3000", "markPrice": "2990", "liqPrice": "3300",
            "leverage": "10", "positionIdx": 0,
        })
        with self.assertRaises(BybitError):
            client.set_trading_stop(short, 2980, 3100)
        with patch.object(client, "request", return_value={"retCode": 0}) as request:
            self.assertEqual(client.set_trading_stop(short, 3050, 2800), "trading-stop")
        request.assert_called_once_with("POST", "/v5/position/trading-stop", body={
            "category": "linear", "symbol": "ETHUSDT", "tpslMode": "Full",
            "positionIdx": 0, "stopLoss": "3050.0", "slTriggerBy": "MarkPrice",
            "takeProfit": "2800.0", "tpTriggerBy": "MarkPrice",
        })

    def test_position_mode_is_normalized(self):
        position = normalize_position({
            "symbol": "BTCUSDT", "side": "Buy", "size": "1", "avgPrice": "60000",
            "markPrice": "60100", "tradeMode": "1", "positionIdx": 1,
        })
        self.assertEqual(position["side"], "long")
        self.assertEqual(position["position_side"], "long")
        self.assertEqual(position["margin_mode"], "isolated")


if __name__ == "__main__":
    unittest.main()
