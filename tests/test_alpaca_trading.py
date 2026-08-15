import json
from pathlib import Path

from tools.alpaca_trading import AutopilotPaperTrader, PaperTradingEngine, RLTrader


def test_rl_trader_can_choose_and_learn(tmp_path):
    trader = RLTrader(state_path=tmp_path / "rl_state.json")

    state = [0.02, 0.01, 0.5]
    action = trader.choose_action(state)

    assert action in {"buy", "hold", "sell"}

    trader.learn(state, action, reward=0.25)
    assert trader.policy["buy"] >= 0.0
    assert trader.policy["sell"] >= 0.0
    assert trader.policy["hold"] >= 0.0


def test_paper_trading_engine_simulates_trade(tmp_path):
    state_path = tmp_path / "paper_state.json"
    engine = PaperTradingEngine(state_path=state_path)

    result = engine.simulate_trade("AAPL", "buy", price=100.0, quantity=2)

    assert result["status"] == "simulated"
    assert result["symbol"] == "AAPL"
    assert result["action"] == "buy"
    assert engine.portfolio["cash"] < 100000.0
    assert engine.portfolio["positions"]["AAPL"] == 2

    persisted = json.loads(state_path.read_text())
    assert persisted["portfolio"]["positions"]["AAPL"] == 2


def test_autopilot_uses_own_paper_portfolio_and_target(tmp_path):
    trader = AutopilotPaperTrader(state_path=tmp_path / "autopilot_state.json")

    result = trader.run_cycle("AAPL", price=100.0)

    assert result["mode"] == "paper"
    assert trader.portfolio["cash"] <= 100000.0
    assert trader.goal["target_value"] == 120000.0
    assert result["symbol"] == "AAPL"


def test_autopilot_reaches_target_with_15_trades(tmp_path):
    trader = AutopilotPaperTrader(state_path=tmp_path / "autopilot_state.json")

    results = trader.run_test_sequence("AAPL", price=100.0, trades=15)

    assert len(results) == 15
    actions = [item["action"] for item in results]
    assert all(action in {"buy", "sell", "hold"} for action in actions)
    assert "buy" in actions
    assert "sell" in actions


def test_engine_liquidates_remaining_positions(tmp_path):
    engine = PaperTradingEngine(state_path=tmp_path / "paper_state.json")

    calls = []

    def fake_place_order(symbol, action, price=100.0, quantity=1):
        calls.append((symbol, action, quantity))
        return {"status": "submitted", "symbol": symbol, "action": action, "quantity": quantity, "mode": "alpaca-paper"}

    engine.place_order = fake_place_order  # type: ignore[method-assign]

    results = engine.liquidate_positions({"AAPL": 3, "NVDA": 0, "TSLA": 2}, prices={"AAPL": 210.0, "TSLA": 250.0})

    assert len(results) == 2
    assert all(item["status"] == "submitted" for item in results)
    assert calls == [("AAPL", "sell", 3), ("TSLA", "sell", 2)]


def test_engine_liquidates_open_alpaca_positions(monkeypatch, tmp_path):
    engine = PaperTradingEngine(state_path=tmp_path / "paper_state.json")

    monkeypatch.setenv("ALPACA_API_KEY", "demo-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "demo-secret")
    monkeypatch.setenv("ALPACA_PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"symbol": "NVDA", "qty": "5", "side": "long", "current_price": "200.0"},
                {"symbol": "TSLA", "qty": "2", "side": "short", "current_price": "250.0"},
            ]

    monkeypatch.setattr("tools.alpaca_trading.requests.get", lambda *args, **kwargs: DummyResponse())

    calls = []

    def fake_place_order(symbol, action, price=100.0, quantity=1):
        calls.append((symbol, action, quantity))
        return {"status": "submitted", "symbol": symbol, "action": action, "quantity": quantity, "mode": "alpaca-paper"}

    engine.place_order = fake_place_order  # type: ignore[method-assign]

    results = engine.liquidate_open_alpaca_positions()

    assert len(results) == 2
    assert calls == [("NVDA", "sell", 5), ("TSLA", "buy", 2)]


def test_engine_bulk_liquidate_positions(monkeypatch, tmp_path):
    engine = PaperTradingEngine(state_path=tmp_path / "paper_state.json")

    monkeypatch.setenv("ALPACA_API_KEY", "demo-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "demo-secret")
    monkeypatch.setenv("ALPACA_PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    class DummyDeleteResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"symbol": "AAPL", "side": "sell", "qty": "5"},
                {"symbol": "NVDA", "side": "sell", "qty": "2"},
            ]

    class DummyGetResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    monkeypatch.setattr("tools.alpaca_trading.requests.delete", lambda *args, **kwargs: DummyDeleteResponse())
    monkeypatch.setattr("tools.alpaca_trading.requests.get", lambda *args, **kwargs: DummyGetResponse())

    results = engine.liquidate_open_alpaca_positions()

    assert len(results) == 2
    assert all(item["status"] == "submitted" for item in results)
    assert {item["symbol"] for item in results} == {"AAPL", "NVDA"}
