import json
import os
import re
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")


class RLTrader:
    """A lightweight reinforcement-learning style policy for paper trading."""

    def __init__(self, state_path: Optional[Path] = None):
        self.state_path = Path(state_path or "data/alpaca_rl_state.json")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = {"buy": 0.33, "hold": 0.34, "sell": 0.33}
        self.learned_weights = {"trend": 0.0, "volatility": 0.0, "momentum": 0.0}
        self._load_state()

    def _load_state(self):
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                self.policy = data.get("policy", self.policy)
                self.learned_weights = data.get("learned_weights", self.learned_weights)
            except Exception:
                self.policy = {"buy": 0.33, "hold": 0.34, "sell": 0.33}

    def _save_state(self):
        self.state_path.write_text(json.dumps({"policy": self.policy, "learned_weights": self.learned_weights}, indent=2))

    def choose_action(self, state: List[float]) -> str:
        trend, volatility, momentum = state
        if trend > 0.02 and momentum > 0.01:
            return "buy"
        if trend < -0.02 and momentum < -0.01:
            return "sell"
        return "hold"

    def learn(self, state: List[float], action: str, reward: float):
        trend, volatility, momentum = state
        self.learned_weights["trend"] += reward * trend
        self.learned_weights["volatility"] += reward * volatility
        self.learned_weights["momentum"] += reward * momentum
        self.policy[action] = max(0.05, self.policy.get(action, 0.33) + reward * 0.1)
        total = sum(self.policy.values())
        for key in self.policy:
            self.policy[key] = self.policy[key] / total
        self._save_state()


class AutopilotPaperTrader:
    """A separate paper-trading autopilot with its own portfolio and simple profit goal."""

    def __init__(self, state_path: Optional[Path] = None):
        self.state_path = Path(state_path or "data/autopilot_paper_state.json")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.portfolio = {"cash": 100000.0, "positions": {}}
        self.goal = {"target_value": 120000.0, "target_reached": False}
        self.rl_trader = RLTrader(self.state_path.with_name("autopilot_rl_state.json"))
        self.last_price = None
        self.watchlist = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META"]
        self.live_prices_enabled = True
        self.price_history: Dict[str, List[float]] = {}
        self.entry_reference: Dict[str, float] = {}
        self.entry_step: Dict[str, int] = {}
        self.position_peak_price: Dict[str, float] = {}
        self.session_peak_value = 100000.0
        self.max_position_fraction = 0.35
        self.max_drawdown_fraction = 0.03
        self.take_profit_fraction = 0.012
        self.stop_loss_fraction = 0.008
        self.trailing_stop_fraction = 0.007
        self._load_state()

    def _load_state(self):
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                self.portfolio = data.get("portfolio", self.portfolio)
                self.goal = data.get("goal", self.goal)
            except Exception:
                self.portfolio = {"cash": 100000.0, "positions": {}}
                self.goal = {"target_value": 120000.0, "target_reached": False}

    def _save_state(self):
        self.state_path.write_text(json.dumps({"portfolio": self.portfolio, "goal": self.goal}, indent=2))

    def _alpaca_market_config(self) -> Dict[str, str]:
        return {
            "api_key": os.getenv("ALPACA_API_KEY", "").strip(),
            "secret_key": os.getenv("ALPACA_SECRET_KEY", "").strip(),
            "base_url": os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip(),
            "data_url": os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").strip(),
        }

    def _fetch_live_prices(self, symbols: List[str]) -> Dict[str, float]:
        if not self.live_prices_enabled:
            return {}
        config = self._alpaca_market_config()
        if not config["api_key"] or not config["secret_key"]:
            return {}
        try:
            response = requests.get(
                f"{config['data_url']}/v2/stocks/quotes/latest",
                params={"symbols": ",".join(symbols)},
                headers={
                    "APCA-API-KEY-ID": config["api_key"],
                    "APCA-API-SECRET-KEY": config["secret_key"],
                },
                timeout=10,
            )
            response.raise_for_status()
            quotes = response.json().get("quotes", {})
            prices: Dict[str, float] = {}
            for symbol, quote in quotes.items():
                bid = float(quote.get("bp") or 0.0)
                ask = float(quote.get("ap") or 0.0)
                if bid > 0 and ask > 0:
                    prices[symbol] = (bid + ask) / 2.0
                elif ask > 0:
                    prices[symbol] = ask
                elif bid > 0:
                    prices[symbol] = bid
            return prices
        except Exception:
            return {}

    def _next_synthetic_prices(self, current_prices: Dict[str, float], step: int) -> Dict[str, float]:
        cycle = [0.95, 1.07, 1.03, 0.98, 1.08, 1.01]
        updated = dict(current_prices)
        for index, symbol in enumerate(updated.keys()):
            multiplier = cycle[(step + index) % len(cycle)]
            updated[symbol] = max(1.0, float(updated[symbol]) * multiplier)
        return updated

    def _total_value(self, prices: Dict[str, float]) -> float:
        value = float(self.portfolio["cash"])
        for symbol, quantity in self.portfolio["positions"].items():
            value += float(quantity) * float(prices.get(symbol, 0.0))
        return value

    def _choose_best_trade(
        self,
        prices: Dict[str, float],
        previous_prices: Dict[str, float],
        base_price: float,
        step: int,
        steps_since_last_trade: int,
    ) -> Dict[str, object]:
        moves: Dict[str, float] = {}
        trends: Dict[str, float] = {}
        momentum_map: Dict[str, float] = {}
        volatility_map: Dict[str, float] = {}
        rsi_map: Dict[str, float] = {}

        for symbol, current_price in prices.items():
            previous_price = float(previous_prices.get(symbol, current_price))
            move = 0.0 if previous_price <= 0 else (float(current_price) - previous_price) / previous_price
            moves[symbol] = move

            history = list(self.price_history.get(symbol, []))
            full_history = history + [float(current_price)]
            short_window = full_history[-4:] if len(full_history) >= 4 else full_history
            long_window = full_history[-12:] if len(full_history) >= 12 else full_history

            short_avg = mean(short_window) if short_window else float(current_price)
            long_avg = mean(long_window) if long_window else float(current_price)
            trends[symbol] = 0.0 if long_avg <= 0 else (short_avg - long_avg) / long_avg

            anchor_index = -6 if len(full_history) >= 6 else 0
            anchor_price = float(full_history[anchor_index]) if full_history else float(current_price)
            momentum_map[symbol] = 0.0 if anchor_price <= 0 else (float(current_price) - anchor_price) / anchor_price

            returns = []
            for idx in range(1, len(full_history)):
                previous = float(full_history[idx - 1])
                current = float(full_history[idx])
                if previous > 0:
                    returns.append((current - previous) / previous)
            recent_returns = returns[-10:] if len(returns) >= 10 else returns
            volatility_map[symbol] = float(pstdev(recent_returns)) if len(recent_returns) >= 2 else abs(move)

            gains = [item for item in recent_returns if item > 0]
            losses = [abs(item) for item in recent_returns if item < 0]
            avg_gain = mean(gains) if gains else 0.0
            avg_loss = mean(losses) if losses else 0.0
            if avg_loss <= 1e-9:
                rsi_map[symbol] = 70.0 if avg_gain > 0 else 50.0
            else:
                rs = avg_gain / avg_loss
                rsi_map[symbol] = 100.0 - (100.0 / (1.0 + rs))

        held_symbols = [s for s, q in self.portfolio["positions"].items() if int(q) > 0]
        total_value = self._total_value(prices)
        self.session_peak_value = max(float(self.session_peak_value), float(total_value))
        drawdown = 0.0
        if self.session_peak_value > 0:
            drawdown = (self.session_peak_value - total_value) / self.session_peak_value

        avg_trend = mean(list(trends.values())) if trends else 0.0
        avg_momentum = mean(list(momentum_map.values())) if momentum_map else 0.0
        avg_volatility = mean(list(volatility_map.values())) if volatility_map else 0.0
        risk_off_mode = drawdown >= self.max_drawdown_fraction or (avg_trend < -0.006 and avg_momentum < -0.004)

        market_regime = "neutral"
        if avg_trend > 0.0015 and avg_momentum > 0.001 and avg_volatility < 0.02:
            market_regime = "trend"
        elif abs(avg_trend) < 0.001 and avg_volatility >= 0.01:
            market_regime = "chop"

        if steps_since_last_trade < 1 and not risk_off_mode:
            return {
                "action": "hold",
                "symbol": next(iter(prices.keys())),
                "score": 0.0,
                "reason": "we are waiting for better confirmation before the next entry or exit",
                "move": 0.0,
                "allocation_fraction": 0.0,
                "sell_fraction": 0.0,
            }

        if held_symbols:
            forced_exits: List[Dict[str, object]] = []
            for symbol in held_symbols:
                entry_price = float(self.entry_reference.get(symbol, prices.get(symbol, 1.0)))
                current_price = float(prices.get(symbol, entry_price))
                pnl = 0.0 if entry_price <= 0 else (current_price - entry_price) / entry_price
                momentum = float(momentum_map.get(symbol, 0.0))
                trend = float(trends.get(symbol, 0.0))
                rsi = float(rsi_map.get(symbol, 50.0))
                peak_price = max(float(self.position_peak_price.get(symbol, current_price)), current_price)
                self.position_peak_price[symbol] = peak_price
                peak_drawdown = 0.0 if peak_price <= 0 else (peak_price - current_price) / peak_price
                held_steps = max(0, step - int(self.entry_step.get(symbol, step)))

                if pnl <= -self.stop_loss_fraction:
                    forced_exits.append(
                        {
                            "symbol": symbol,
                            "score": abs(pnl),
                            "reason": "risk control triggered a stop-loss to protect capital",
                            "sell_fraction": 1.0,
                        }
                    )
                    continue
                if peak_drawdown >= self.trailing_stop_fraction and pnl > -0.003:
                    forced_exits.append(
                        {
                            "symbol": symbol,
                            "score": peak_drawdown,
                            "reason": "trailing stop protected gains after momentum weakened",
                            "sell_fraction": 0.75,
                        }
                    )
                    continue
                if pnl >= self.take_profit_fraction and momentum <= 0.0:
                    forced_exits.append(
                        {
                            "symbol": symbol,
                            "score": pnl,
                            "reason": "take-profit locked gains as momentum started fading",
                            "sell_fraction": 0.75,
                        }
                    )
                    continue
                if held_steps >= 18 and pnl < 0.003 and rsi >= 58.0:
                    forced_exits.append(
                        {
                            "symbol": symbol,
                            "score": float(held_steps) * 0.001,
                            "reason": "stale position was recycled to free capital for better setups",
                            "sell_fraction": 0.5,
                        }
                    )
                    continue
                if risk_off_mode and (trend < 0.0 or momentum < 0.0):
                    forced_exits.append(
                        {
                            "symbol": symbol,
                            "score": abs(momentum) + abs(trend),
                            "reason": "market regime turned defensive so we reduced exposure",
                            "sell_fraction": 0.75,
                        }
                    )

            if forced_exits:
                best_exit = max(forced_exits, key=lambda item: float(item.get("score", 0.0)))
                symbol = str(best_exit["symbol"])
                return {
                    "action": "sell",
                    "symbol": symbol,
                    "score": float(best_exit.get("score", 0.0)),
                    "reason": str(best_exit["reason"]),
                    "move": float(moves.get(symbol, 0.0)),
                    "allocation_fraction": 0.0,
                    "sell_fraction": float(best_exit.get("sell_fraction", 0.5)),
                }

            # Controlled periodic rebalance helps lock gains and prevents one-way exposure.
            rebalance_period = 8 if market_regime == "trend" else 6
            if step % rebalance_period == 0:
                rebalance_symbol = max(held_symbols, key=lambda s: moves.get(s, 0.0))
                return {
                    "action": "sell",
                    "symbol": rebalance_symbol,
                    "score": float(moves.get(rebalance_symbol, 0.0)),
                    "reason": "we rebalanced exposure to lock gains and control risk",
                    "move": float(moves.get(rebalance_symbol, 0.0)),
                    "allocation_fraction": 0.0,
                    "sell_fraction": 0.5,
                }

        if risk_off_mode:
            return {
                "action": "hold",
                "symbol": next(iter(prices.keys())),
                "score": 0.0,
                "reason": "drawdown and trend signals are defensive so we are waiting",
                "move": 0.0,
                "allocation_fraction": 0.0,
                "sell_fraction": 0.0,
            }

        cash = float(self.portfolio["cash"])
        if cash < min(prices.values()):
            return {
                "action": "hold",
                "symbol": next(iter(prices.keys())),
                "score": 0.0,
                "reason": "cash is too low for a new entry so we are waiting",
                "move": 0.0,
                "allocation_fraction": 0.0,
                "sell_fraction": 0.0,
            }

        candidates: List[Dict[str, object]] = []
        for symbol in prices.keys():
            trend = float(trends.get(symbol, 0.0))
            move = float(moves.get(symbol, 0.0))
            momentum = float(momentum_map.get(symbol, 0.0))
            volatility = float(volatility_map.get(symbol, 0.0))
            rsi = float(rsi_map.get(symbol, 50.0))

            trend_score = (trend * 2.0) + (momentum * 1.1) - (volatility * 1.8)
            reversion_score = ((-move) * 1.5) + ((50.0 - rsi) / 100.0) - (volatility * 1.2)
            score = trend_score if market_regime == "trend" else reversion_score if market_regime == "chop" else (trend_score + reversion_score) / 2.0

            pullback_in_uptrend = trend > 0.001 and move < -0.0007 and momentum > -0.01 and 35.0 <= rsi <= 62.0
            breakout_follow = trend > 0.003 and momentum > 0.004 and move > 0.0 and rsi <= 68.0
            mean_reversion_bounce = move <= -0.003 and rsi <= 38.0 and momentum >= -0.02

            existing_qty = int(self.portfolio["positions"].get(symbol, 0))
            current_notional = existing_qty * float(prices[symbol])
            allocation_used = 0.0 if total_value <= 0 else current_notional / total_value
            within_position_limit = allocation_used < self.max_position_fraction

            allowed_setup = pullback_in_uptrend or breakout_follow
            if market_regime == "chop":
                allowed_setup = mean_reversion_bounce

            if within_position_limit and allowed_setup:
                base_allocation = 0.07 if market_regime == "chop" else 0.09
                conviction_boost = min(0.07, max(0.0, score * 4.0))
                volatility_penalty = min(0.05, volatility * 6.0)
                allocation = max(0.03, min(0.18, base_allocation + conviction_boost - volatility_penalty))
                candidates.append(
                    {
                        "symbol": symbol,
                        "score": score,
                        "move": move,
                        "allocation_fraction": allocation,
                    }
                )

        if candidates:
            best = max(candidates, key=lambda item: float(item["score"]))
            chosen_symbol = str(best["symbol"])
            return {
                "action": "buy",
                "symbol": chosen_symbol,
                "score": float(best["score"]),
                "reason": "trend and momentum aligned with a favorable entry setup",
                "move": float(best["move"]),
                "allocation_fraction": float(best["allocation_fraction"]),
                "sell_fraction": 0.0,
            }

        # Keep some exploration alive so the strategy does not freeze in narrow windows.
        if not held_symbols and step % 5 == 0 and cash >= min(prices.values()) and not risk_off_mode:
            exploratory_symbol = max(
                prices.keys(),
                key=lambda s: float(trends.get(s, 0.0)) + float(momentum_map.get(s, 0.0)) - float(volatility_map.get(s, 0.0)),
            )
            return {
                "action": "buy",
                "symbol": exploratory_symbol,
                "score": 0.001,
                "reason": "we took a small exploratory entry to sample momentum",
                "move": float(moves.get(exploratory_symbol, 0.0)),
                "allocation_fraction": 0.05,
                "sell_fraction": 0.0,
            }

        return {
            "action": "hold",
            "symbol": next(iter(prices.keys())),
            "score": 0.0,
            "reason": "setup was unclear so we waited for a clearer edge",
            "move": 0.0,
            "allocation_fraction": 0.0,
            "sell_fraction": 0.0,
        }

    def reset_portfolio(self, start_cash: float = 100000.0, target_value: float = 120000.0):
        self.portfolio = {"cash": float(start_cash), "positions": {}}
        self.goal = {"target_value": float(target_value), "target_reached": False}
        self.last_price = None
        self.price_history = {}
        self.entry_reference = {}
        self.entry_step = {}
        self.position_peak_price = {}
        self.session_peak_value = float(start_cash)
        self._save_state()

    def run_cycle(self, symbol: str, price: float, quantity: int = 1) -> Dict[str, object]:
        price = max(float(price), 1.0)
        quantity = max(1, int(quantity))
        current_value = self.portfolio["cash"] + sum(self.portfolio["positions"].get(sym, 0) * price for sym in self.portfolio["positions"])
        target_gap = self.goal["target_value"] - current_value
        previous_price = self.last_price
        price_change = 0.0
        if previous_price is not None and previous_price > 0:
            price_change = (price - previous_price) / previous_price

        action = "hold"
        reason = "price action was flat and we stayed defensive"
        if price_change > 0.03:
            action = "buy"
            reason = "price was rising fast and the trend looked strong"
        elif price_change < -0.03 and self.portfolio["positions"]:
            action = "sell"
            reason = "price was falling and we reduced risk"

        executed_quantity = 0
        if action == "buy":
            budget = self.portfolio["cash"] * 0.25
            quantity = max(1, int(budget // price))
            cost = price * quantity
            if self.portfolio["cash"] >= cost:
                self.portfolio["cash"] -= cost
                self.portfolio["positions"][symbol] = self.portfolio["positions"].get(symbol, 0) + quantity
                executed_quantity = quantity
            else:
                action = "hold"
                reason = "not enough paper cash for the planned position"
        elif action == "sell":
            existing = self.portfolio["positions"].get(symbol, 0)
            if existing >= 1:
                sell_quantity = max(1, existing // 2)
                self.portfolio["positions"][symbol] = existing - sell_quantity
                self.portfolio["cash"] += price * sell_quantity
                executed_quantity = sell_quantity
            else:
                action = "hold"
                reason = "no matching shares to sell"

        total_value = self.portfolio["cash"] + sum(self.portfolio["positions"].get(sym, 0) * price for sym in self.portfolio["positions"])
        self.goal["target_reached"] = total_value >= self.goal["target_value"]
        reward = 0.35 if action == "buy" and total_value > current_value else 0.2 if action == "sell" and total_value > current_value else -0.1
        self.rl_trader.learn([price_change, abs(price_change), 0.03], action, reward)
        self.last_price = price
        self._save_state()
        return {
            "status": "paper-autopilot",
            "mode": "paper",
            "symbol": symbol,
            "price": price,
            "action": action,
            "quantity": executed_quantity,
            "reason": reason,
            "portfolio": dict(self.portfolio),
            "goal": dict(self.goal),
            "total_value": total_value,
            "target_gap": target_gap,
            "learned": {
                "policy": dict(self.rl_trader.policy),
                "weights": dict(self.rl_trader.learned_weights),
            },
        }

    def run_test_sequence(self, symbol: str = "AAPL", price: float = 100.0, trades: int = 15) -> List[Dict[str, object]]:
        results = []
        self.reset_portfolio(start_cash=100000.0, target_value=120000.0)

        watchlist: List[str] = []
        for candidate in [symbol] + self.watchlist:
            if candidate not in watchlist:
                watchlist.append(candidate)

        current_prices = {
            ticker: float(price) * (1.0 + 0.06 * index)
            for index, ticker in enumerate(watchlist)
        }
        self.price_history = {ticker: [float(ticker_price)] for ticker, ticker_price in current_prices.items()}
        last_trade_step = -5

        for index in range(trades):
            previous_prices = dict(current_prices)
            live_prices = self._fetch_live_prices(watchlist)
            if live_prices:
                for ticker, latest_price in live_prices.items():
                    if ticker in current_prices:
                        current_prices[ticker] = max(1.0, float(latest_price))
            else:
                current_prices = self._next_synthetic_prices(current_prices, index)

            for ticker, latest_price in current_prices.items():
                history = self.price_history.setdefault(ticker, [])
                history.append(float(latest_price))
                if len(history) > 40:
                    del history[:-40]

            decision = self._choose_best_trade(
                current_prices,
                previous_prices,
                float(price),
                index,
                index - last_trade_step,
            )
            chosen_symbol = str(decision["symbol"])
            action = str(decision["action"])
            selected_price = float(current_prices[chosen_symbol])
            move = float(decision.get("move", 0.0))
            allocation_fraction = float(decision.get("allocation_fraction", 0.08))
            sell_fraction = float(decision.get("sell_fraction", 0.5))

            current_value = self._total_value(current_prices)
            target_gap = self.goal["target_value"] - current_value
            executed_quantity = 0
            reason = str(decision["reason"])

            if action == "buy":
                budget = self.portfolio["cash"] * max(0.03, min(0.20, allocation_fraction))
                quantity = max(1, int(budget // selected_price))
                cost = selected_price * quantity
                if self.portfolio["cash"] >= cost:
                    self.portfolio["cash"] -= cost
                    self.portfolio["positions"][chosen_symbol] = self.portfolio["positions"].get(chosen_symbol, 0) + quantity
                    if self.portfolio["positions"][chosen_symbol] > 0:
                        self.entry_reference[chosen_symbol] = selected_price
                        self.entry_step[chosen_symbol] = index
                        self.position_peak_price[chosen_symbol] = max(
                            float(self.position_peak_price.get(chosen_symbol, selected_price)),
                            selected_price,
                        )
                    executed_quantity = quantity
                    last_trade_step = index
                else:
                    action = "hold"
                    reason = "cash was too low for the planned entry"
            elif action == "sell":
                existing = int(self.portfolio["positions"].get(chosen_symbol, 0))
                if existing >= 1:
                    quantity = max(1, int(existing * max(0.25, min(1.0, sell_fraction))))
                    self.portfolio["positions"][chosen_symbol] = existing - quantity
                    self.portfolio["cash"] += selected_price * quantity
                    if self.portfolio["positions"][chosen_symbol] <= 0 and chosen_symbol in self.entry_reference:
                        del self.entry_reference[chosen_symbol]
                    if self.portfolio["positions"][chosen_symbol] <= 0 and chosen_symbol in self.entry_step:
                        del self.entry_step[chosen_symbol]
                    if self.portfolio["positions"][chosen_symbol] <= 0 and chosen_symbol in self.position_peak_price:
                        del self.position_peak_price[chosen_symbol]
                    executed_quantity = quantity
                    last_trade_step = index
                else:
                    action = "hold"
                    reason = "we had no shares in that symbol to sell"

            total_value = self._total_value(current_prices)
            self.goal["target_reached"] = total_value >= self.goal["target_value"]
            reward = 0.35 if total_value > current_value else -0.1
            self.rl_trader.learn([move, abs(move), 0.03], action, reward)
            self.last_price = selected_price

            result = {
                "status": "paper-autopilot",
                "mode": "paper",
                "symbol": chosen_symbol,
                "price": selected_price,
                "action": action,
                "quantity": executed_quantity,
                "reason": reason,
                "portfolio": dict(self.portfolio),
                "goal": dict(self.goal),
                "total_value": total_value,
                "target_gap": target_gap,
                "watchlist": list(watchlist),
                "watched_prices": {k: round(v, 4) for k, v in current_prices.items()},
                "learned": {
                    "policy": dict(self.rl_trader.policy),
                    "weights": dict(self.rl_trader.learned_weights),
                },
            }
            results.append(result)

        self._save_state()
        return results


class PaperTradingEngine:
    """Paper trading engine that simulates Alpaca-style orders and optionally uses real Alpaca credentials if present."""

    def __init__(self, state_path: Optional[Path] = None):
        self.state_path = Path(state_path or "data/paper_trading_state.json")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.portfolio = {"cash": 100000.0, "positions": {}}
        self.rl_state_path = self.state_path.with_name("alpaca_rl_state.json")
        self.rl_trader = RLTrader(self.rl_state_path)
        self.last_shadow_positions: Dict[str, int] = {}
        self._load_state()

    def _load_state(self):
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                self.portfolio = data.get("portfolio", self.portfolio)
            except Exception:
                self.portfolio = {"cash": 100000.0, "positions": {}}

    def _save_state(self):
        self.state_path.write_text(json.dumps({"portfolio": self.portfolio, "rl_state": {"policy": self.rl_trader.policy, "learned_weights": self.rl_trader.learned_weights}}, indent=2))

    def _alpaca_config(self) -> Dict[str, str]:
        return {
            "api_key": os.getenv("ALPACA_API_KEY", "").strip(),
            "secret_key": os.getenv("ALPACA_SECRET_KEY", "").strip(),
            "paper": os.getenv("ALPACA_PAPER_TRADING", "true").strip().lower() in {"1", "true", "yes", "on"},
            "base_url": os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip(),
        }

    def get_alpaca_account_summary(self) -> Dict[str, object]:
        config = self._alpaca_config()
        if not (config["api_key"] and config["secret_key"] and config["paper"]):
            return {"mode": "paper-local", "available": False}

        try:
            response = requests.get(
                f"{config['base_url']}/v2/account",
                headers={
                    "APCA-API-KEY-ID": config["api_key"],
                    "APCA-API-SECRET-KEY": config["secret_key"],
                },
                timeout=10,
            )
            response.raise_for_status()
            account = response.json()
            return {
                "mode": "alpaca-paper",
                "available": True,
                "equity": float(account.get("equity") or 0.0),
                "last_equity": float(account.get("last_equity") or 0.0),
                "cash": float(account.get("cash") or 0.0),
                "buying_power": float(account.get("buying_power") or 0.0),
            }
        except Exception:
            return {"mode": "alpaca-paper", "available": False}

    def simulate_trade(self, symbol: str, action: str, price: float, quantity: int) -> Dict[str, object]:
        if action not in {"buy", "sell", "hold"}:
            raise ValueError("action must be buy, sell, or hold")
        if action == "hold":
            return {"status": "simulated", "symbol": symbol, "action": action, "quantity": 0, "price": price}

        cost = price * quantity
        if action == "buy":
            if cost > self.portfolio["cash"]:
                raise ValueError("insufficient paper cash")
            self.portfolio["cash"] -= cost
            existing = self.portfolio["positions"].get(symbol, 0)
            self.portfolio["positions"][symbol] = existing + quantity
        elif action == "sell":
            existing = self.portfolio["positions"].get(symbol, 0)
            if existing < quantity:
                raise ValueError("insufficient paper shares")
            self.portfolio["positions"][symbol] = existing - quantity
            self.portfolio["cash"] += cost

        self._save_state()
        return {"status": "simulated", "symbol": symbol, "action": action, "quantity": quantity, "price": price, "cash": self.portfolio["cash"]}

    def place_order(self, symbol: str, action: str, price: float = 100.0, quantity: int = 1) -> Dict[str, object]:
        config = self._alpaca_config()
        if config["api_key"] and config["secret_key"] and config["paper"]:
            try:
                response = requests.post(
                    f"{config['base_url']}/v2/orders",
                    json={
                        "symbol": symbol,
                        "qty": str(quantity),
                        "side": action,
                        "type": "market",
                        "time_in_force": "day",
                    },
                    headers={
                        "APCA-API-KEY-ID": config["api_key"],
                        "APCA-API-SECRET-KEY": config["secret_key"],
                    },
                    timeout=10,
                )
                response.raise_for_status()
                return {"status": "submitted", "symbol": symbol, "action": action, "quantity": quantity, "price": price, "mode": "alpaca-paper"}
            except Exception:
                try:
                    return self.simulate_trade(symbol, action, price, quantity)
                except ValueError:
                    return {
                        "status": "skipped",
                        "symbol": symbol,
                        "action": action,
                        "quantity": 0,
                        "price": price,
                        "mode": "paper",
                    }
        try:
            return self.simulate_trade(symbol, action, price, quantity)
        except ValueError:
            return {
                "status": "skipped",
                "symbol": symbol,
                "action": action,
                "quantity": 0,
                "price": price,
                "mode": "paper",
            }

    def submit_autopilot_sequence(self, decisions: List[Dict[str, object]], quantity_per_trade: int = 1) -> List[Dict[str, object]]:
        """Submit autopilot decisions to Alpaca paper (or simulation fallback) so broker-side charts update."""
        submissions: List[Dict[str, object]] = []
        shadow_positions: Dict[str, int] = {}
        for index, decision in enumerate(decisions, start=1):
            action = str(decision.get("action", "hold")).lower()
            symbol = str(decision.get("symbol", "AAPL"))
            price = float(decision.get("price", 100.0))
            sequence_qty = int(decision.get("quantity", 0) or 0)
            planned_qty = max(1, int(quantity_per_trade))
            trade_qty = sequence_qty if sequence_qty > 0 else planned_qty

            if action == "sell" and int(shadow_positions.get(symbol, 0)) < 1:
                submissions.append(
                    {
                        "trade_number": index,
                        "symbol": symbol,
                        "action": action,
                        "status": "skipped",
                        "quantity": 0,
                    }
                )
                continue
            if action not in {"buy", "sell"}:
                submissions.append(
                    {
                        "trade_number": index,
                        "symbol": symbol,
                        "action": action,
                        "status": "skipped",
                        "quantity": 0,
                    }
                )
                continue

            if action == "sell":
                trade_qty = min(trade_qty, int(shadow_positions.get(symbol, 0)))
                if trade_qty < 1:
                    submissions.append(
                        {
                            "trade_number": index,
                            "symbol": symbol,
                            "action": action,
                            "status": "skipped",
                            "quantity": 0,
                        }
                    )
                    continue

            result = self.place_order(symbol, action, price=price, quantity=trade_qty)
            if result.get("status") in {"submitted", "simulated"}:
                if action == "buy":
                    shadow_positions[symbol] = int(shadow_positions.get(symbol, 0)) + int(trade_qty)
                elif action == "sell":
                    shadow_positions[symbol] = max(0, int(shadow_positions.get(symbol, 0)) - int(trade_qty))
            submissions.append(
                {
                    "trade_number": index,
                    "symbol": symbol,
                    "action": action,
                    "status": result.get("status", "unknown"),
                    "quantity": result.get("quantity", trade_qty),
                    "mode": result.get("mode", "paper"),
                }
            )
            self.last_shadow_positions = dict(shadow_positions)
        self.last_shadow_positions = dict(shadow_positions)
        return submissions

    def liquidate_positions(self, positions: Dict[str, int], prices: Optional[Dict[str, float]] = None) -> List[Dict[str, object]]:
        """Submit sell orders for all remaining session positions to close out a profitable session."""
        prices = prices or {}
        liquidation_results: List[Dict[str, object]] = []
        for symbol, quantity in positions.items():
            qty = max(0, int(quantity))
            if qty < 1:
                continue
            price = float(prices.get(symbol, 100.0))
            result = self.place_order(symbol, "sell", price=price, quantity=qty)
            liquidation_results.append(
                {
                    "symbol": symbol,
                    "quantity": qty,
                    "status": result.get("status", "unknown"),
                    "mode": result.get("mode", "paper"),
                }
            )
        return liquidation_results

    def liquidate_open_alpaca_positions(self) -> List[Dict[str, object]]:
        """Close all current Alpaca paper positions so the account is flat after a profitable session."""
        config = self._alpaca_config()
        if not (config["api_key"] and config["secret_key"] and config["paper"]):
            return []

        headers = {
            "APCA-API-KEY-ID": config["api_key"],
            "APCA-API-SECRET-KEY": config["secret_key"],
        }

        # Prefer broker-native bulk close. This is the most reliable way to flatten account positions.
        liquidation_results: List[Dict[str, object]] = []
        try:
            bulk_response = requests.delete(
                f"{config['base_url']}/v2/positions",
                headers=headers,
                params={"cancel_orders": "true"},
                timeout=15,
            )
            if bulk_response.status_code in {200, 207}:
                data = bulk_response.json()
                if isinstance(data, list):
                    for item in data:
                        symbol = str(item.get("symbol", "")).strip()
                        side = str(item.get("side", "sell")).lower()
                        qty = item.get("qty") or item.get("quantity") or 0
                        liquidation_results.append(
                            {
                                "symbol": symbol,
                                "quantity": int(float(qty)) if str(qty) else 0,
                                "action": side,
                                "status": "submitted",
                                "mode": "alpaca-paper",
                                "source": "alpaca-bulk-close",
                            }
                        )
        except Exception:
            pass

        try:
            response = requests.get(
                f"{config['base_url']}/v2/positions",
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            positions = response.json()
        except Exception:
            return []

        # Second pass: explicitly close any positions that survived bulk close.
        for position in positions:
            symbol = str(position.get("symbol", "")).strip()
            if not symbol:
                continue

            qty_raw = position.get("qty_available") or position.get("qty") or "0"
            try:
                qty = int(abs(float(qty_raw)))
            except Exception:
                qty = 0
            if qty < 1:
                continue

            side = str(position.get("side", "long")).lower()
            action = "buy" if side == "short" else "sell"
            try:
                price = float(position.get("current_price") or 100.0)
            except Exception:
                price = 100.0

            try:
                close_response = requests.delete(
                    f"{config['base_url']}/v2/positions/{symbol}",
                    headers=headers,
                    params={"qty": str(qty)},
                    timeout=10,
                )
                close_response.raise_for_status()
                status = "submitted"
                mode = "alpaca-paper"
            except Exception:
                result = self.place_order(symbol, action, price=price, quantity=qty)
                status = result.get("status", "unknown")
                mode = result.get("mode", "paper")

            liquidation_results.append(
                {
                    "symbol": symbol,
                    "quantity": qty,
                    "action": action,
                    "status": status,
                    "mode": mode,
                    "source": "alpaca-position-close",
                }
            )

        return liquidation_results

    def get_portfolio_summary(self) -> Dict[str, object]:
        return {
            "cash": self.portfolio["cash"],
            "positions": dict(self.portfolio["positions"]),
            "mode": "paper"
        }


def build_trading_prompt(command: str, engine: Optional[PaperTradingEngine] = None) -> str:
    summary = engine.get_portfolio_summary() if engine else {"cash": 100000.0, "positions": {}, "mode": "paper"}
    return (
        "You are running a paper trading assistant for Alpaca. "
        f"Current portfolio: cash={summary['cash']}, positions={summary['positions']}. "
        "If the user asks to buy, sell, or analyze a stock, respond with a paper-trading plan and use the trading tools when available."
    )


def handle_trading_command(command: str, engine: Optional[PaperTradingEngine] = None) -> str:
    engine = engine or PaperTradingEngine()
    command_lower = command.lower()

    if any(keyword in command_lower for keyword in ["portfolio", "balance", "positions", "summary"]):
        summary = engine.get_portfolio_summary()
        return f"Paper trading summary: cash=${summary['cash']:.2f}, positions={summary['positions']}."

    explicit_action = None
    if "buy" in command_lower:
        explicit_action = "buy"
    elif "sell" in command_lower:
        explicit_action = "sell"
    elif "hold" in command_lower:
        explicit_action = "hold"

    symbol_match = None
    for token in re.findall(r"\b[A-Z]{1,5}\b", command.upper()):
        if token not in {"BUY", "SELL", "HOLD", "PORTFOLIO", "THE", "AND", "FOR"}:
            symbol_match = token
            break
    if not symbol_match and "apple" in command_lower:
        symbol_match = "AAPL"
    if not symbol_match and "tesla" in command_lower:
        symbol_match = "TSLA"
    if not symbol_match and "nvidia" in command_lower:
        symbol_match = "NVDA"

    if not symbol_match:
        symbol_match = "AAPL"

    quantity = 1
    quantity_match = re.search(r"(\d+)\s*(share|shares|stock|stocks)", command_lower)
    if quantity_match:
        quantity = int(quantity_match.group(1))

    price = 100.0
    price_match = re.search(r"\$(\d+(?:\.\d+)?)", command)
    if price_match:
        price = float(price_match.group(1))

    if explicit_action is None:
        state = [0.0, 0.0, 0.0]
        explicit_action = engine.rl_trader.choose_action(state)

    result = engine.place_order(symbol_match, explicit_action, price=price, quantity=quantity)
    engine.rl_trader.learn([0.01, 0.0, 0.01], explicit_action, 0.1 if result["status"] in {"simulated", "submitted"} else -0.2)
    summary = engine.get_portfolio_summary()
    return (
        f"Paper trading update: {explicit_action.upper()} {symbol_match} {quantity} share(s) at ${price:.2f}. "
        f"Mode={result['status']}; cash=${summary['cash']:.2f}; positions={summary['positions']}. "
        "The RL policy was updated so future sessions can learn from this paper trade."
    )
