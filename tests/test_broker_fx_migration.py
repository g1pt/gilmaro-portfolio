from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
import os

import pytest

import broker_adapter
from broker_adapter import (
    BrokerOrderResult,
    MT5BrokerAdapter,
    NormalizedPosition,
    SymbolSpecs,
    calculate_fx_lot_size,
    normalize_mt5_symbol_info,
    resolve_fx_pip_value_per_lot,
)
from execution.risk_manager import RiskState
from paper_trader import (
    LiveSafetyController,
    TrailingPolicy,
    classify_execution_failure,
    close_bybit_position,
    compute_live_position_metrics,
    compute_exit_plan,
    enforce_live_spread_gate,
    ensure_exchange_protection,
    execute_live_entry_flow,
    mt5_preflight_check,
    resolve_contract_size,
    sync_bot_with_exchange_position,
)


class MockBrokerAdapter:
    def __init__(self) -> None:
        self.position = NormalizedPosition(symbol='EURUSD', side='FLAT', size=0.0, entry_price=0.0)
        self.protection_ok = True
        self.protection_offset = 0.0
        self.close_failures_remaining = 0
        self.persist_after_close = False
        self.close_calls = 0
        self.last_close: dict[str, object] | None = None

    def connect(self) -> bool:
        return True

    def get_symbol_specs(self, symbol: str) -> SymbolSpecs:
        return SymbolSpecs(
            symbol=symbol,
            category='fx',
            qty_step=0.01,
            min_qty=0.01,
            tick_size=0.00001,
            digits=5,
            point=0.00001,
            pip_size=0.0001,
            contract_size=100000,
            lot_step=0.01,
            min_lot=0.01,
            max_lot=5.0,
            pip_value_per_lot=10.0,
        )

    def fetch_current_tick(self, symbol: str) -> dict[str, float]:
        return {'bid': 1.1000, 'ask': 1.1002, 'last': 1.1001}

    def fetch_open_positions(self, symbol: str | None = None) -> list[NormalizedPosition]:
        return [self.position] if self.position.is_open else []

    def fetch_open_position(self, symbol: str) -> NormalizedPosition:
        return self.position if self.position.symbol == symbol else NormalizedPosition(symbol=symbol, side='FLAT', size=0.0, entry_price=0.0)

    def get_open_orders(self, category: str, symbol: str) -> dict[str, object]:
        return {'result': {'list': []}}

    def place_market_order(self, symbol: str, side: str, qty: float, **kwargs: object) -> BrokerOrderResult:
        entry_price = 1.1002 if side == 'LONG' else 1.1000
        self.position = NormalizedPosition(symbol=symbol, side=side, size=qty, entry_price=entry_price, broker_id='1001')
        return BrokerOrderResult(True, '1001', side, qty, entry_price, {'symbol': symbol})

    def close_position(self, symbol: str, side: str, qty: float, **kwargs: object) -> BrokerOrderResult:
        self.close_calls += 1
        self.last_close = {'symbol': symbol, 'side': side, 'qty': qty, **kwargs}
        if self.close_failures_remaining > 0:
            self.close_failures_remaining -= 1
            return BrokerOrderResult(False, None, side, qty, None, {'symbol': symbol}, 'close_rejected')
        if not self.persist_after_close:
            self.position = NormalizedPosition(symbol=symbol, side='FLAT', size=0.0, entry_price=0.0)
        return BrokerOrderResult(True, '2001', side, qty, 1.1000, {'symbol': symbol})

    def set_protection(self, symbol: str, side: str, take_profit_price: float, stop_loss_price: float, **kwargs: object) -> bool:
        if not self.protection_ok or not self.position.is_open:
            return False
        self.position = NormalizedPosition(
            symbol=self.position.symbol,
            side=self.position.side,
            size=self.position.size,
            entry_price=self.position.entry_price,
            stop_loss=stop_loss_price + self.protection_offset,
            take_profit=take_profit_price + self.protection_offset,
            broker_id=self.position.broker_id,
            raw={'side': side},
        )
        return True

    def verify_protection(self, symbol: str, expected_tp: float, expected_sl: float, tick_size: float) -> tuple[bool, NormalizedPosition]:
        tolerance = max(tick_size, 1e-9)
        ok = self.position.take_profit is not None and self.position.stop_loss is not None and abs(self.position.take_profit - expected_tp) <= tolerance and abs(self.position.stop_loss - expected_sl) <= tolerance
        return ok, self.position

    def sync_position_state(self, symbol: str) -> NormalizedPosition:
        return self.fetch_open_position(symbol)


def test_normalize_mt5_symbol_info_builds_fx_specs() -> None:
    info = SimpleNamespace(point=0.00001, digits=5, volume_step=0.01, volume_min=0.01, volume_max=50.0, trade_contract_size=100000, trade_mode=4)

    specs = normalize_mt5_symbol_info('EURUSD', info)

    assert specs.symbol == 'EURUSD'
    assert specs.category == 'fx'
    assert specs.pip_size == pytest.approx(0.0001)
    assert specs.contract_size == pytest.approx(100000)
    assert specs.lot_step == pytest.approx(0.01)


def test_calculate_fx_lot_size_rounds_safely_to_broker_rules() -> None:
    specs = SymbolSpecs(
        symbol='EURUSD',
        category='fx',
        qty_step=0.01,
        min_qty=0.01,
        tick_size=0.00001,
        digits=5,
        point=0.00001,
        pip_size=0.0001,
        contract_size=100000,
        lot_step=0.01,
        min_lot=0.01,
        max_lot=100.0,
        pip_value_per_lot=10.0,
    )

    lots = calculate_fx_lot_size(
        risk_per_trade_usd=100.0,
        risk_pct=None,
        account_equity_usd=10000.0,
        entry_price=1.1000,
        stop_loss_price=1.0950,
        specs=specs,
    )

    assert lots == pytest.approx(0.19)


def test_calculate_fx_lot_size_blocks_zero_lot_after_rounding() -> None:
    specs = SymbolSpecs(
        symbol='EURUSD',
        category='fx',
        qty_step=0.1,
        min_qty=0.1,
        tick_size=0.00001,
        digits=5,
        point=0.00001,
        pip_size=0.0001,
        contract_size=100000,
        lot_step=0.1,
        min_lot=0.1,
        max_lot=1.0,
        pip_value_per_lot=10.0,
    )

    with pytest.raises(ValueError, match='zero'):
        calculate_fx_lot_size(
            risk_per_trade_usd=1.0,
            risk_pct=None,
            account_equity_usd=10000.0,
            entry_price=1.1000,
            stop_loss_price=1.0900,
            specs=specs,
        )


def test_sync_bot_with_broker_position_recovers_flat_internal_state() -> None:
    broker = MockBrokerAdapter()
    broker.position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')
    risk_state = RiskState(open_positions=0)

    result = sync_bot_with_exchange_position(
        session=broker,
        symbol='EURUSD',
        active_trade=None,
        active_notional_usd=0.0,
        active_position_scale=1.0,
        risk_state=risk_state,
        safety_controller=LiveSafetyController(),
    )

    assert result.recovered is True
    assert result.active_trade is not None
    assert result.active_trade.side == 'LONG'
    assert risk_state.open_positions == 1


def test_ensure_exchange_protection_verifies_fx_levels() -> None:
    broker = MockBrokerAdapter()
    broker.position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    trade.entry_time = datetime.now(timezone.utc)
    safety = LiveSafetyController()

    ok = ensure_exchange_protection(
        session=broker,
        symbol='EURUSD',
        trade=trade,
        exchange_position=broker.position,
        specs=broker.get_symbol_specs('EURUSD'),
        safety_controller=safety,
        trailing_policy=TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False),
    )

    assert ok is True
    assert safety.live_trading_halted is False
    assert broker.position.take_profit is not None
    assert broker.position.stop_loss is not None


def test_close_position_reduce_only_path_works_for_adapter() -> None:
    broker = MockBrokerAdapter()
    broker.position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')

    result = close_bybit_position(broker, 'EURUSD', 'LONG', 0.2, position_idx=1001)

    assert result.success is True
    assert broker.close_calls == 1
    assert broker.last_close is not None
    assert broker.last_close['position'] == 1001


def test_kill_switch_on_desync_with_adapter() -> None:
    broker = MockBrokerAdapter()
    broker.position = NormalizedPosition(symbol='EURUSD', side='SHORT', size=0.5, entry_price=1.1000, broker_id='1001')
    risk_state = RiskState(open_positions=1)
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    safety = LiveSafetyController(max_exchange_desync_count=1)

    result = sync_bot_with_exchange_position(
        session=broker,
        symbol='EURUSD',
        active_trade=trade,
        active_notional_usd=1000.0,
        active_position_scale=1.0,
        risk_state=risk_state,
        safety_controller=safety,
    )

    assert result.mismatch is True
    assert result.safe_mode_triggered is True
    assert safety.live_trading_halted is True


def test_single_position_enforcement_kill_switch() -> None:
    safety = LiveSafetyController(max_position_qty=0.1)

    allowed = safety.enforce_position_limits(position_value_usd=500.0, qty=0.2)

    assert allowed is False
    assert safety.live_trading_halted is True


def test_resolve_fx_pip_value_per_lot_keeps_eurusd_correct() -> None:
    specs = SymbolSpecs(
        symbol='EURUSD',
        category='fx',
        qty_step=0.01,
        min_qty=0.01,
        tick_size=0.00001,
        digits=5,
        point=0.00001,
        pip_size=0.0001,
        contract_size=100000,
        lot_step=0.01,
        min_lot=0.01,
        max_lot=100.0,
        pip_value_per_lot=None,
        account_currency='USD',
    )

    pip_value = resolve_fx_pip_value_per_lot(specs=specs, entry_price=1.1000)

    assert pip_value == pytest.approx(10.0)


def test_resolve_fx_pip_value_per_lot_handles_usdjpy_safely() -> None:
    specs = SymbolSpecs(
        symbol='USDJPY',
        category='fx',
        qty_step=0.01,
        min_qty=0.01,
        tick_size=0.001,
        digits=3,
        point=0.001,
        pip_size=0.01,
        contract_size=100000,
        lot_step=0.01,
        min_lot=0.01,
        max_lot=100.0,
        pip_value_per_lot=None,
        account_currency='USD',
    )

    pip_value = resolve_fx_pip_value_per_lot(specs=specs, entry_price=150.0)

    assert pip_value == pytest.approx(1000.0 / 150.0)


def test_mt5_filling_mode_selection_falls_back_to_ioc(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        symbol_info=lambda symbol: SimpleNamespace(filling_mode=1),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True

    assert adapter._resolve_filling_mode('EURUSD') == 1


def test_mt5_adapter_uses_active_session_without_password_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MT5_LOGIN', raising=False)
    monkeypatch.delenv('MT5_PASSWORD', raising=False)
    monkeypatch.delenv('MT5_SERVER', raising=False)

    adapter = MT5BrokerAdapter()

    assert adapter.login == 0
    assert adapter.password == ''
    assert adapter.server == ''


def test_mt5_adapter_requires_password_when_login_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MT5_LOGIN', '123456')
    monkeypatch.delenv('MT5_PASSWORD', raising=False)
    monkeypatch.setenv('MT5_SERVER', 'Demo-Server')

    with pytest.raises(ValueError, match='MT5_PASSWORD missing'):
        MT5BrokerAdapter()


def test_mt5_send_order_with_fallback_retries_on_invalid_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    class FakeResult:
        def __init__(self, retcode: int, comment: str = 'ok', order: int = 0, price: float = 0.0) -> None:
            self.retcode = retcode
            self.comment = comment
            self.order = order
            self.price = price

        def _asdict(self) -> dict[str, object]:
            return {'retcode': self.retcode, 'comment': self.comment, 'order': self.order, 'price': self.price}

    submitted_modes: list[int] = []

    def fake_order_send(request: dict[str, object]) -> FakeResult:
        mode = int(request['type_filling'])
        submitted_modes.append(mode)
        if mode == 2:
            return FakeResult(10030, comment='Unsupported filling mode')
        return FakeResult(10009, comment='done', order=1234, price=1.2345)

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        TRADE_RETCODE_DONE=10009,
        TRADE_RETCODE_INVALID_FILL=10030,
        symbol_info=lambda symbol: SimpleNamespace(filling_mode=2),
        order_send=fake_order_send,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0,
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.2344, 'ask': 1.2345, 'last': 1.23445})

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is True
    assert submitted_modes[:2] == [2, 1]
    assert adapter.filling_mode_cache['EURUSD'] == 1


def test_mt5_cached_filling_mode_used_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    submitted_modes: list[int] = []

    def fake_order_send(request: dict[str, object]) -> SimpleNamespace:
        submitted_modes.append(int(request['type_filling']))
        return SimpleNamespace(retcode=10009, comment='done', order=1234, price=1.1111, _asdict=lambda: {'retcode': 10009, 'comment': 'done'})

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        TRADE_RETCODE_DONE=10009,
        TRADE_RETCODE_INVALID_FILL=10030,
        symbol_info=lambda symbol: SimpleNamespace(filling_mode=2),
        order_send=fake_order_send,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0,
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    adapter.filling_mode_cache['EURUSD'] = 1
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.1110, 'ask': 1.1111, 'last': 1.11105})

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is True
    assert submitted_modes[0] == 1


def test_mt5_place_market_order_blocks_non_tradable_trade_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_MODE_DISABLED=0,
        SYMBOL_TRADE_MODE_LONGONLY=1,
        SYMBOL_TRADE_MODE_SHORTONLY=2,
        SYMBOL_TRADE_MODE_CLOSEONLY=3,
        SYMBOL_TRADE_MODE_FULL=4,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        ORDER_TIME_GTC=0,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode=1,
            trade_mode=2,
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is False
    assert result.reason == 'symbol_short_only'
    assert adapter.connected is False
    assert adapter.last_error == 'symbol_short_only:EURUSD'


def test_mt5_place_market_order_blocks_invalid_filling_mode_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_MODE_FULL=4,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        ORDER_TIME_GTC=0,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode='bad',
            trade_mode=4,
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is False
    assert result.reason == 'invalid_filling_mode'
    assert adapter.connected is False
    assert adapter.last_error == 'invalid_filling_mode:EURUSD'


def test_mt5_place_market_order_blocks_when_symbol_select_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_MODE_FULL=4,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        ORDER_TIME_GTC=0,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode=1,
            trade_mode=4,
            visible=False,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is False
    assert result.reason == 'symbol_select_unavailable'
    assert adapter.connected is False
    assert adapter.last_error == 'symbol_select_unavailable:EURUSD'


def test_mt5_close_request_short_position_uses_buy_and_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        TRADE_ACTION_DEAL=1,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        ORDER_TIME_GTC=0,
        symbol_info=lambda symbol: SimpleNamespace(volume_step=0.01, volume_min=0.01, volume_max=100.0),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.31770, 'ask': 1.31789, 'last': 1.31780})

    request, close_side = adapter.build_mt5_close_request(
        symbol='GBPUSD',
        position_side='SHORT',
        qty=0.011,
        position_ticket='4394085335',
        comment='hftbot-close',
    )

    assert close_side == 'LONG'
    assert request is not None
    assert request['type'] == 0
    assert request['price'] == pytest.approx(1.31789)
    assert request['position'] == 4394085335
    assert request['volume'] == pytest.approx(0.01)


def test_mt5_close_request_long_position_uses_sell_and_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        TRADE_ACTION_DEAL=1,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        ORDER_TIME_GTC=0,
        symbol_info=lambda symbol: SimpleNamespace(volume_step=0.01, volume_min=0.01, volume_max=100.0),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.10234, 'ask': 1.10249, 'last': 1.10241})

    request, close_side = adapter.build_mt5_close_request(
        symbol='EURUSD',
        position_side='LONG',
        qty=0.02,
        position_ticket=12345,
        comment='hftbot-close',
    )

    assert close_side == 'SHORT'
    assert request is not None
    assert request['type'] == 1
    assert request['price'] == pytest.approx(1.10234)


def test_mt5_send_order_with_fallback_stops_on_invalid_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    class FakeResult:
        def __init__(self, retcode: int, comment: str = 'ok') -> None:
            self.retcode = retcode
            self.comment = comment
            self.order = 111
            self.price = 1.2222

        def _asdict(self) -> dict[str, object]:
            return {'retcode': self.retcode, 'comment': self.comment}

    submitted_modes: list[int] = []

    def fake_order_send(request: dict[str, object]) -> FakeResult:
        mode = int(request['type_filling'])
        submitted_modes.append(mode)
        if len(submitted_modes) == 1:
            return FakeResult(10013, comment='Invalid request')
        return FakeResult(10009, comment='done')

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        TRADE_RETCODE_DONE=10009,
        TRADE_RETCODE_INVALID_FILL=10030,
        TRADE_RETCODE_INVALID=10013,
        symbol_info=lambda symbol: SimpleNamespace(filling_mode=2),
        order_send=fake_order_send,
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    result, used_mode = adapter.send_order_with_fallback(
        request={'symbol': 'GBPUSD', 'type_filling': 2, 'price': 1.2222},
        symbol='GBPUSD',
        symbol_info=fake_mt5.symbol_info('GBPUSD'),
        requested_mode=2,
    )

    assert result is not None
    assert int(result.retcode) == 10013
    assert submitted_modes == [2]
    assert used_mode == 2


def test_mt5_place_market_order_blocks_after_three_failures_during_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode=1,
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1),
        order_send=lambda req: SimpleNamespace(retcode=10013, comment='Invalid request'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.2000, 'ask': 1.2002, 'last': 1.2001})

    for _ in range(3):
        result = adapter.place_market_order('EURUSD', 'LONG', 0.01)
        assert result.success is False

    blocked = adapter.place_market_order('EURUSD', 'LONG', 0.01)
    assert blocked.success is False
    assert blocked.reason == 'cooldown'


def test_mt5_place_market_order_resets_failure_counter_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode=1,
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1),
        order_send=lambda req: SimpleNamespace(retcode=10013, comment='Invalid request'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)
    monkeypatch.setattr(broker_adapter.time, 'time', lambda: 1000.0)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.2000, 'ask': 1.2002, 'last': 1.2001})
    adapter.execution_fail_cache['EURUSD_LONG'] = {'fails': 3, 'last_fail': 900.0}

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is False
    assert result.reason != 'cooldown'
    assert adapter.execution_fail_cache['EURUSD_LONG']['fails'] == 1
    assert adapter.execution_fail_cache['EURUSD_LONG']['last_fail'] == 1000.0


def test_mt5_place_market_order_blocks_when_account_info_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_MODE_FULL=4,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode=1,
            trade_mode=4,
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: None,
        last_error=lambda: (-10005, 'IPC initialize failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.2000, 'ask': 1.2002, 'last': 1.2001})

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is False
    assert result.reason == 'account_info_unavailable'
    assert adapter.connected is False
    assert adapter.last_error == '-10005:IPC initialize failed'


def test_mt5_place_market_order_blocks_invalid_account_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_MODE_FULL=4,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode=1,
            trade_mode=4,
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1, server='Demo-Server', balance=1000.0, equity='bad', margin_free=980.25),
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.2000, 'ask': 1.2002, 'last': 1.2001})

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is False
    assert result.reason == 'invalid_account_info_equity'
    assert adapter.connected is False
    assert adapter.last_error == 'invalid_account_info:equity'


def test_mt5_place_market_order_invalid_retcode_response_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_MODE_FULL=4,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode=1,
            trade_mode=4,
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1, server='Demo-Server', balance=1000.0, equity=995.5, margin_free=980.25),
        order_send=lambda req: SimpleNamespace(retcode='bad', comment='weird response'),
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.2000, 'ask': 1.2002, 'last': 1.2001})

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is False
    assert result.reason == 'invalid_retcode'
    assert adapter.connected is False
    assert adapter.last_error == 'invalid_retcode:EURUSD'


def test_mt5_place_market_order_success_without_order_identifier_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_MODE_FULL=4,
        TRADE_RETCODE_DONE=10009,
        TRADE_ACTION_DEAL=1,
        ORDER_TIME_GTC=0,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        symbol_info=lambda symbol: SimpleNamespace(
            filling_mode=1,
            trade_mode=4,
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100.0,
        ),
        positions_total=lambda: 0,
        account_info=lambda: SimpleNamespace(login=1, server='Demo-Server', balance=1000.0, equity=995.5, margin_free=980.25),
        order_send=lambda req: SimpleNamespace(retcode=10009, comment='done', order=0, deal=None, price=1.2345),
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    monkeypatch.setattr(adapter, 'fetch_current_tick', lambda symbol: {'bid': 1.2000, 'ask': 1.2002, 'last': 1.2001})

    result = adapter.place_market_order('EURUSD', 'LONG', 0.01)

    assert result.success is False
    assert result.reason == 'invalid_order_id'
    assert adapter.connected is False
    assert adapter.last_error == 'invalid_order_id:EURUSD'


def test_mt5_close_position_invalid_retcode_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        TRADE_RETCODE_DONE=10009,
        symbol_info=lambda symbol: SimpleNamespace(),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    open_position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')
    monkeypatch.setattr(adapter, 'fetch_open_position', lambda symbol: open_position)
    monkeypatch.setattr(adapter, 'build_mt5_close_request', lambda **kwargs: ({'symbol': 'EURUSD', 'volume': 0.2, 'position': 1001}, 'SHORT'))
    monkeypatch.setattr(adapter, 'send_order_with_fallback', lambda **kwargs: (SimpleNamespace(retcode='bad', comment='weird close', price='bad'), 1))

    result = adapter.close_position('EURUSD', 'LONG', 0.2)

    assert result.success is False
    assert result.reason == 'invalid_retcode'
    assert adapter.connected is False
    assert adapter.last_error == 'invalid_close_retcode:EURUSD'


def test_mt5_close_position_allows_missing_order_identifier_when_flat_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        TRADE_RETCODE_DONE=10009,
        symbol_info=lambda symbol: SimpleNamespace(),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    states = [
        NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001'),
        NormalizedPosition(symbol='EURUSD', side='FLAT', size=0.0, entry_price=0.0),
    ]

    def fake_fetch_open_position(symbol: str) -> NormalizedPosition:
        if len(states) > 1:
            return states.pop(0)
        return states[0]

    monkeypatch.setattr(adapter, 'fetch_open_position', fake_fetch_open_position)
    monkeypatch.setattr(adapter, 'build_mt5_close_request', lambda **kwargs: ({'symbol': 'EURUSD', 'volume': 0.2, 'position': 1001}, 'SHORT'))
    monkeypatch.setattr(adapter, 'send_order_with_fallback', lambda **kwargs: (SimpleNamespace(retcode=10009, comment='done', order=0, deal=None, price=1.1001), 1))

    result = adapter.close_position('EURUSD', 'LONG', 0.2)

    assert result.success is True
    assert result.order_id is None
    assert result.reason is None


def test_mt5_set_protection_invalid_retcode_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        TRADE_ACTION_SLTP=6,
        TRADE_RETCODE_DONE=10009,
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        order_send=lambda request: SimpleNamespace(retcode='bad', comment='weird protection'),
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True

    ok = adapter.set_protection('EURUSD', 'LONG', 1.1010, 1.0990, position=1001)

    assert ok is False
    assert adapter.connected is False
    assert adapter.last_error == 'invalid_protection_retcode:EURUSD'


def test_mt5_set_protection_none_response_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        TRADE_ACTION_SLTP=6,
        TRADE_RETCODE_DONE=10009,
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        order_send=lambda request: None,
        last_error=lambda: (-10005, 'IPC initialize failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = True

    ok = adapter.set_protection('EURUSD', 'LONG', 1.1010, 1.0990, position=1001)

    assert ok is False
    assert adapter.connected is False
    assert adapter.last_error == 'protection_result_none:EURUSD'


def test_mt5_connect_retries_and_sets_last_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import broker_adapter

    terminal_path = tmp_path / 'terminal64.exe'
    terminal_path.write_text('stub')
    monkeypatch.setenv('MT5_PATH', str(terminal_path))
    monkeypatch.setenv('MT5_LOGIN', '123456')
    monkeypatch.setenv('MT5_PASSWORD', 'secret')
    monkeypatch.setenv('MT5_SERVER', 'Demo-Server')

    calls = {'initialize': 0, 'shutdown': 0}

    def fake_initialize(**kwargs: object) -> bool:
        calls['initialize'] += 1
        assert kwargs == {
            'path': str(terminal_path),
            'timeout': 180000,
        }
        return False

    fake_mt5 = SimpleNamespace(
        initialize=fake_initialize,
        shutdown=lambda: calls.__setitem__('shutdown', calls['shutdown'] + 1),
        account_info=lambda: None,
        last_error=lambda: (-10005, 'IPC initialize failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)
    monkeypatch.setattr(broker_adapter.time, 'sleep', lambda *_args, **_kwargs: None)

    adapter = MT5BrokerAdapter()

    assert adapter.connect() is False
    assert adapter.connected is False
    assert adapter.last_error == "(-10005, 'IPC initialize failed')"
    assert calls['initialize'] == 3
    assert calls['shutdown'] == 3


def test_mt5_account_snapshot_never_returns_partial_state_when_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(last_error=lambda: (-10005, 'IPC initialize failed'))
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = MT5BrokerAdapter()
    adapter.connected = False
    adapter.last_error = "(-10005, 'IPC initialize failed')"

    snapshot = adapter.get_mt5_account_snapshot()

    assert snapshot == {
        'connected': False,
        'reason': "(-10005, 'IPC initialize failed')",
        'login': None,
        'server': None,
        'balance': None,
        'equity': None,
        'positions_total': 0,
    }


def test_mt5_account_snapshot_account_info_none_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: None,
        last_error=lambda: (-10005, 'IPC initialize failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    snapshot = adapter.get_mt5_account_snapshot()

    assert snapshot == {
        'connected': False,
        'reason': '-10005:IPC initialize failed',
        'login': None,
        'server': None,
        'balance': None,
        'equity': None,
        'positions_total': 0,
    }
    assert adapter.connected is False
    assert adapter.last_error == '-10005:IPC initialize failed'


def test_mt5_account_snapshot_invalid_positions_total_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server', balance=1000.0, equity=995.5, margin_free=980.25),
        positions_total=lambda: 'bad',
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    snapshot = adapter.get_mt5_account_snapshot()

    assert snapshot == {
        'connected': False,
        'reason': 'invalid_positions_total:bad',
        'login': None,
        'server': None,
        'balance': None,
        'equity': None,
        'positions_total': 0,
    }
    assert adapter.connected is False
    assert adapter.last_error == 'invalid_positions_total:bad'


def test_mt5_account_snapshot_invalid_account_numeric_field_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login='bad', server='Demo-Server', balance=1000.0, equity=995.5, margin_free=980.25),
        positions_total=lambda: 1,
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    snapshot = adapter.get_mt5_account_snapshot()

    assert snapshot == {
        'connected': False,
        'reason': 'invalid_account_info:login',
        'login': None,
        'server': None,
        'balance': None,
        'equity': None,
        'positions_total': 0,
    }
    assert adapter.connected is False
    assert adapter.last_error == 'invalid_account_info:login'


def test_mt5_connect_failure_clears_stale_ready_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import broker_adapter

    terminal_path = tmp_path / 'terminal64.exe'
    terminal_path.write_text('stub')
    monkeypatch.setenv('MT5_PATH', str(terminal_path))
    monkeypatch.setenv('MT5_LOGIN', '123456')
    monkeypatch.setenv('MT5_PASSWORD', 'secret')
    monkeypatch.setenv('MT5_SERVER', 'Demo-Server')

    fake_mt5 = SimpleNamespace(
        initialize=lambda **kwargs: False,
        shutdown=lambda: None,
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        last_error=lambda: (-10005, 'IPC initialize failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)
    monkeypatch.setattr(broker_adapter.time, 'sleep', lambda *_args, **_kwargs: None)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    assert adapter.connect() is False
    assert adapter.connected is False
    assert adapter.last_error == "(-10005, 'IPC initialize failed')"


def test_mt5_connect_success_restores_healthy_adapter_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import broker_adapter

    terminal_path = tmp_path / 'terminal64.exe'
    terminal_path.write_text('stub')
    monkeypatch.setenv('MT5_PATH', str(terminal_path))
    monkeypatch.setenv('MT5_LOGIN', '123456')
    monkeypatch.setenv('MT5_PASSWORD', 'secret')
    monkeypatch.setenv('MT5_SERVER', 'Demo-Server')

    fake_mt5 = SimpleNamespace(
        initialize=lambda **kwargs: True,
        shutdown=lambda: None,
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server', balance=1000.0, equity=995.5, margin_free=980.25),
        positions_total=lambda: 1,
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)
    monkeypatch.setattr(broker_adapter.time, 'sleep', lambda *_args, **_kwargs: None)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = False
    adapter.last_error = 'stale_error'

    assert adapter.connect() is True
    assert adapter.connected is True
    assert adapter.last_error is None
    assert adapter.get_mt5_account_snapshot() == {
        'connected': True,
        'reason': None,
        'login': 123456,
        'server': 'Demo-Server',
        'balance': 1000.0,
        'equity': 995.5,
        'free_margin': 980.25,
        'positions_total': 1,
    }


def test_mt5_fetch_open_positions_none_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        positions_get=lambda **kwargs: None,
        last_error=lambda: (-10005, 'IPC initialize failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    with pytest.raises(RuntimeError, match='positions_get failed'):
        adapter.fetch_open_positions('EURUSD')

    assert adapter.connected is False
    assert adapter.last_error == '-10005:IPC initialize failed'


def test_mt5_get_symbol_specs_none_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        symbol_info=lambda symbol: None,
        last_error=lambda: (-10018, 'symbol not found'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    with pytest.raises(RuntimeError, match='symbol_info unavailable'):
        adapter.get_symbol_specs('EURUSD')

    assert adapter.connected is False
    assert adapter.last_error == 'symbol_info_unavailable:EURUSD'


def test_mt5_fetch_current_tick_invalid_values_collapse_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        symbol_info=lambda symbol: SimpleNamespace(visible=True),
        symbol_info_tick=lambda symbol: SimpleNamespace(bid=0.0, ask=0.0, last=0.0),
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    with pytest.raises(RuntimeError, match='invalid tick values'):
        adapter.fetch_current_tick('EURUSD')

    assert adapter.connected is False
    assert adapter.last_error == 'invalid_tick_values:EURUSD'


def test_mt5_fetch_current_tick_stale_quote_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    monkeypatch.setenv('MT5_MAX_TICK_AGE_SECONDS', '30')
    monkeypatch.setattr(broker_adapter.time, 'time', lambda: 1000.0)

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        symbol_info=lambda symbol: SimpleNamespace(visible=True),
        symbol_info_tick=lambda symbol: SimpleNamespace(bid=1.1000, ask=1.1002, last=1.1001, time_msc=900000),
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    with pytest.raises(RuntimeError, match='stale tick'):
        adapter.fetch_current_tick('EURUSD')

    assert adapter.connected is False
    assert adapter.last_error == 'stale_tick:EURUSD'


def test_mt5_validate_order_channel_invalid_positions_total_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        positions_total=lambda: 'bad',
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    with pytest.raises(RuntimeError, match='positions_total invalid'):
        adapter.validate_order_channel()

    assert adapter.connected is False
    assert adapter.last_error == 'invalid_positions_total:bad'


def test_mt5_get_open_orders_none_collapses_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        orders_get=lambda: None,
        last_error=lambda: (-10005, 'IPC initialize failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True
    adapter.last_error = None

    with pytest.raises(RuntimeError, match='orders_get failed'):
        adapter.get_open_orders('EURUSD')

    assert adapter.connected is False
    assert adapter.last_error == '-10005:IPC initialize failed'


def test_spread_gate_blocks_abnormally_wide_mt5_spread(caplog: pytest.LogCaptureFixture) -> None:
    broker = MockBrokerAdapter()
    broker.fetch_current_tick = lambda symbol: {'bid': 1.1000, 'ask': 1.1100, 'last': 1.1050}  # type: ignore[method-assign]
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    safety = LiveSafetyController()

    with caplog.at_level('WARNING'):
        allowed = enforce_live_spread_gate(
            session=broker,
            symbol='EURUSD',
            trade=trade,
            specs=broker.get_symbol_specs('EURUSD'),
            max_spread_ratio=0.003,
            safety_controller=safety,
        )

    assert allowed is False
    assert 'LIVE SPREAD BLOCK' in caplog.text
    assert safety.live_trading_halted is False


def test_ensure_exchange_protection_uses_strict_fx_tolerance() -> None:
    broker = MockBrokerAdapter()
    broker.protection_offset = 0.00002
    broker.position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    trade.entry_time = datetime.now(timezone.utc)
    safety = LiveSafetyController()

    ok = ensure_exchange_protection(
        session=broker,
        symbol='EURUSD',
        trade=trade,
        exchange_position=broker.position,
        specs=broker.get_symbol_specs('EURUSD'),
        safety_controller=safety,
        trailing_policy=TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False),
    )

    assert ok is False
    assert safety.live_trading_halted is True


def test_close_position_uses_ticket_and_verifies_post_close_state() -> None:
    broker = MockBrokerAdapter()
    broker.position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')

    result = close_bybit_position(broker, 'EURUSD', 'LONG', 0.2)

    assert result.success is True
    assert broker.last_close is not None
    assert broker.last_close['position'] == 1001
    assert broker.fetch_open_position('EURUSD').is_open is False


def test_close_position_flags_ambiguous_mt5_close() -> None:
    broker = MockBrokerAdapter()
    broker.persist_after_close = True
    broker.position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')

    result = close_bybit_position(broker, 'EURUSD', 'LONG', 0.2)

    assert result.success is False
    assert result.reason == 'close_position_still_open'


def test_close_position_failure_is_treated_as_success_when_position_is_flat() -> None:
    broker = MockBrokerAdapter()
    broker.position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')

    def _failing_close(symbol: str, side: str, qty: float, **kwargs: object) -> BrokerOrderResult:
        broker.position = NormalizedPosition(symbol=symbol, side='FLAT', size=0.0, entry_price=0.0)
        return BrokerOrderResult(False, None, side, qty, None, {'symbol': symbol}, 'invalid_request')

    broker.close_position = _failing_close  # type: ignore[method-assign]
    result = close_bybit_position(broker, 'EURUSD', 'LONG', 0.2)

    assert result.success is True
    assert result.reason is None


def test_close_position_partial_is_reported_with_remaining_qty() -> None:
    broker = MockBrokerAdapter()
    broker.position = NormalizedPosition(symbol='EURUSD', side='LONG', size=0.2, entry_price=1.1000, broker_id='1001')

    def _partial_close(symbol: str, side: str, qty: float, **kwargs: object) -> BrokerOrderResult:
        broker.position = NormalizedPosition(symbol=symbol, side='LONG', size=0.05, entry_price=1.1000, broker_id='1001')
        return BrokerOrderResult(True, '2001', side, qty, 1.1001, {'symbol': symbol}, None)

    broker.close_position = _partial_close  # type: ignore[method-assign]
    result = close_bybit_position(broker, 'EURUSD', 'LONG', 0.2)

    assert result.success is False
    assert result.reason == 'close_position_partial'
    assert result.raw_response.get('remaining_qty') == pytest.approx(0.05)


def test_execution_kill_switch_triggers_after_three_consecutive_failures() -> None:
    safety = LiveSafetyController()

    safety.register_execution_failure('first')
    safety.register_execution_failure('second')
    assert safety.live_trading_halted is False

    safety.register_execution_failure('third')
    assert safety.live_trading_halted is True
    assert safety.halt_reason == 'execution_failure_limit:third'


def test_execute_live_entry_flow_uses_fx_risk_model_when_enabled() -> None:
    broker = MockBrokerAdapter()
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    risk_state = RiskState(open_positions=0)
    safety = LiveSafetyController(max_position_qty=2000.0, max_position_value_usd=5_000_000.0)

    order_result, exchange_position, safe_qty = execute_live_entry_flow(
        session=broker,
        symbol='EURUSD',
        trade=trade,
        position_value_usd=1000.0,
        position_scale=1.0,
        specs=broker.get_symbol_specs('EURUSD'),
        risk_state=risk_state,
        safety_controller=safety,
        trailing_policy=TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False),
        risk_per_trade_usd=100.0,
        account_equity_usd=10000.0,
        use_fx_risk_model=True,
    )

    expected_qty = calculate_fx_lot_size(
        risk_per_trade_usd=100.0,
        risk_pct=None,
        account_equity_usd=10000.0,
        entry_price=trade.entry_price,
        stop_loss_price=trade.entry_price * (1.0 - trade.sl_pct),
        specs=broker.get_symbol_specs('EURUSD'),
    )

    assert order_result is not None
    assert order_result.success is True
    assert exchange_position is not None
    assert exchange_position.is_open is True
    assert safe_qty == pytest.approx(expected_qty)
    assert exchange_position.qty == pytest.approx(expected_qty)
    assert safety.live_trading_halted is False

def test_execute_live_entry_flow_blocks_on_spread_gate() -> None:
    broker = MockBrokerAdapter()
    broker.fetch_current_tick = lambda symbol: {'bid': 1.1000, 'ask': 1.1100, 'last': 1.1050}  # type: ignore[method-assign]
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    risk_state = RiskState(open_positions=0)
    safety = LiveSafetyController(max_position_qty=1.0)

    order_result, exchange_position, safe_qty = execute_live_entry_flow(
        session=broker,
        symbol='EURUSD',
        trade=trade,
        position_value_usd=100.0,
        position_scale=1.0,
        specs=broker.get_symbol_specs('EURUSD'),
        risk_state=risk_state,
        safety_controller=safety,
        trailing_policy=TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False),
    )

    assert order_result is None
    assert exchange_position is None
    assert safe_qty is None
    assert broker.close_calls == 0


def test_execute_live_entry_flow_pre_adjusts_risk_when_fx_risk_is_below_broker_minimum(caplog: pytest.LogCaptureFixture) -> None:
    broker = MockBrokerAdapter()
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    risk_state = RiskState(open_positions=0)
    safety = LiveSafetyController(max_consecutive_execution_failures=1)

    with caplog.at_level('INFO'):
        order_result, exchange_position, safe_qty = execute_live_entry_flow(
            session=broker,
            symbol='EURUSD',
            trade=trade,
            position_value_usd=1000.0,
            position_scale=1.0,
            specs=broker.get_symbol_specs('EURUSD'),
            risk_state=risk_state,
            safety_controller=safety,
            trailing_policy=TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False),
            risk_per_trade_usd=0.1,
            account_equity_usd=10000.0,
            use_fx_risk_model=True,
        )

    assert order_result is not None
    assert order_result.success is True
    assert exchange_position is not None
    assert safe_qty is not None
    assert safe_qty >= broker.get_symbol_specs('EURUSD').minimum_size
    assert safety.live_trading_halted is False
    assert safety.consecutive_execution_failures == 0
    assert 'PRE-ADJUST LOT | old_risk=' in caplog.text


def test_execute_live_entry_flow_real_order_failure_registers_execution_failure() -> None:
    broker = MockBrokerAdapter()
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    risk_state = RiskState(open_positions=0)
    safety = LiveSafetyController(max_consecutive_execution_failures=2, max_position_qty=2.0, max_position_value_usd=5000.0)

    def _reject_order(symbol: str, side: str, qty: float, **kwargs: object) -> BrokerOrderResult:
        return BrokerOrderResult(False, None, side, qty, None, {'symbol': symbol, 'kwargs': kwargs}, 'order_send_rejected')

    broker.place_market_order = _reject_order  # type: ignore[method-assign]
    order_result, exchange_position, safe_qty = execute_live_entry_flow(
        session=broker,
        symbol='EURUSD',
        trade=trade,
        position_value_usd=1000.0,
        position_scale=1.0,
        specs=broker.get_symbol_specs('EURUSD'),
        risk_state=risk_state,
        safety_controller=safety,
        trailing_policy=TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False),
        risk_per_trade_usd=100.0,
        account_equity_usd=10000.0,
        use_fx_risk_model=True,
    )

    assert order_result is not None
    assert order_result.success is False
    assert exchange_position is None
    assert safe_qty is not None
    assert safety.consecutive_execution_failures == 1
    assert safety.live_trading_halted is False
    assert safety.halt_reason is None


def test_mt5_preflight_check_blocks_invalid_lot_step() -> None:
    broker = MockBrokerAdapter()
    specs = broker.get_symbol_specs('EURUSD')

    ok, reason, diagnostics = mt5_preflight_check(
        session=broker,
        symbol='EURUSD',
        side='LONG',
        qty=0.015,
        entry_price=1.1000,
        stop_loss_price=1.0990,
        take_profit_price=1.1010,
        specs=specs,
        account_equity_usd=10000.0,
    )

    assert ok is False
    assert reason == 'qty_not_aligned_to_step'
    assert diagnostics['qty_step'] == pytest.approx(specs.qty_step)


def test_compute_live_position_metrics_uses_symbol_contract_size_rules() -> None:
    fx_specs = MockBrokerAdapter().get_symbol_specs('EURUSD')
    xau_specs = SymbolSpecs(
        symbol='XAUUSD',
        category='metal',
        qty_step=0.01,
        min_qty=0.01,
        tick_size=0.01,
        digits=2,
        point=0.01,
        pip_size=0.01,
        contract_size=100,
        lot_step=0.01,
        min_lot=0.01,
        max_lot=10.0,
        pip_value_per_lot=1.0,
    )

    fx_metrics = compute_live_position_metrics('EURUSD', qty=0.2, entry_price=1.1, specs=fx_specs, leverage=100.0)
    xau_metrics = compute_live_position_metrics('XAUUSD', qty=0.2, entry_price=2300.0, specs=xau_specs, leverage=100.0)

    assert resolve_contract_size('EURUSD', fx_specs) == pytest.approx(100000.0)
    assert resolve_contract_size('XAUUSD', xau_specs) == pytest.approx(100.0)
    assert fx_metrics.notional_value_usd == pytest.approx(22000.0)
    assert xau_metrics.notional_value_usd == pytest.approx(46000.0)


def test_execute_live_entry_flow_uses_broker_confirmed_qty_for_notional_tracking() -> None:
    broker = MockBrokerAdapter()
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    risk_state = RiskState(open_positions=0)
    safety = LiveSafetyController(max_position_qty=2000.0, max_position_value_usd=5_000_000.0)

    order_result, exchange_position, safe_qty = execute_live_entry_flow(
        session=broker,
        symbol='EURUSD',
        trade=trade,
        position_value_usd=1000.0,
        position_scale=1.0,
        specs=broker.get_symbol_specs('EURUSD'),
        risk_state=risk_state,
        safety_controller=safety,
        trailing_policy=TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False),
        risk_per_trade_usd=100.0,
        account_equity_usd=10000.0,
        use_fx_risk_model=True,
    )

    assert order_result is not None and order_result.success
    assert exchange_position is not None and safe_qty is not None
    expected_metrics = compute_live_position_metrics(
        'EURUSD',
        qty=exchange_position.qty,
        entry_price=exchange_position.entry_price,
        specs=broker.get_symbol_specs('EURUSD'),
        leverage=100.0,
    )
    assert expected_metrics.qty == pytest.approx(exchange_position.qty)
    assert expected_metrics.notional_value_usd > 0.0


def test_mt5_preflight_check_blocks_when_margin_exceeds_equity() -> None:
    broker = MockBrokerAdapter()
    specs = broker.get_symbol_specs('EURUSD')

    ok, reason, diagnostics = mt5_preflight_check(
        session=broker,
        symbol='EURUSD',
        side='LONG',
        qty=1.0,
        entry_price=1.2,
        stop_loss_price=1.19,
        take_profit_price=1.21,
        specs=specs,
        account_equity_usd=50.0,
    )

    assert ok is False
    assert reason == 'insufficient_margin'
    assert float(diagnostics['estimated_margin']) > 50.0


def test_classify_execution_failure_maps_margin_errors() -> None:
    assert classify_execution_failure('No money') == 'insufficient_margin'
    assert classify_execution_failure('Unsupported filling mode') == 'unsupported_filling_mode'
    assert classify_execution_failure('order_send_rejected') == 'broker_rejected'


def test_execute_live_entry_flow_mt5_disconnect_triggers_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server'),
        positions_get=lambda symbol=None: [],
        positions_total=lambda: None,
        last_error=lambda: (-10005, 'IPC initialize failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)
    monkeypatch.setattr('paper_trader.enforce_live_spread_gate', lambda **_kwargs: True)
    monkeypatch.setattr('paper_trader._has_duplicate_open_order', lambda *_args, **_kwargs: False)
    monkeypatch.setattr('paper_trader.close_bybit_position', lambda *args, **kwargs: BrokerOrderResult(True, '0', 'SHORT', 0.0, None, {}, None))

    adapter = MT5BrokerAdapter()
    adapter.connected = True
    trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side='LONG', entry_price=1.1000, source='runtime')
    risk_state = RiskState(open_positions=0)
    safety = LiveSafetyController(max_position_qty=2000.0, max_position_value_usd=5_000_000.0)

    with pytest.raises(SystemExit, match='kill switch engaged'):
        execute_live_entry_flow(
            session=adapter,
            symbol='EURUSD',
            trade=trade,
            position_value_usd=1000.0,
            position_scale=1.0,
            specs=SymbolSpecs(
                symbol='EURUSD',
                category='fx',
                qty_step=0.01,
                min_qty=0.01,
                tick_size=0.00001,
                digits=5,
                point=0.00001,
                pip_size=0.0001,
                contract_size=100000,
                lot_step=0.01,
                min_lot=0.01,
                max_lot=5.0,
                pip_value_per_lot=10.0,
            ),
            risk_state=risk_state,
            safety_controller=safety,
            trailing_policy=TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False),
            use_fx_risk_model=False,
        )

    assert safety.live_trading_halted is True
    assert safety.halt_reason is not None
    assert 'mt5_positions_total_failed' in safety.halt_reason


def test_mt5_connect_allows_account_identity_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    terminal_path = tmp_path / 'terminal64.exe'
    terminal_path.write_text('stub')
    monkeypatch.setenv('MT5_PATH', str(terminal_path))
    monkeypatch.setenv('MT5_LOGIN', '123456')
    monkeypatch.setenv('MT5_PASSWORD', 'secret')
    monkeypatch.setenv('MT5_SERVER', 'Demo-Server')

    fake_mt5 = SimpleNamespace(
        initialize=lambda **kwargs: True,
        shutdown=lambda: None,
        account_info=lambda: SimpleNamespace(login=999999, server='Wrong-Server', balance=1000.0, equity=995.0),
        last_error=lambda: (-1, 'validation failed'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)
    monkeypatch.setattr(broker_adapter.time, 'sleep', lambda *_args, **_kwargs: None)

    adapter = broker_adapter.MT5BrokerAdapter()

    assert adapter.connect() is True
    assert adapter.connected is True
    assert adapter.last_error is None


def test_mt5_account_snapshot_returns_complete_state_when_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    import broker_adapter

    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(login=123456, server='Demo-Server', balance=1000.0, equity=995.5, margin_free=980.25),
        positions_total=lambda: 2,
        last_error=lambda: (0, 'ok'),
    )
    monkeypatch.setattr(broker_adapter, 'mt5', fake_mt5)

    adapter = broker_adapter.MT5BrokerAdapter()
    adapter.connected = True

    snapshot = adapter.get_mt5_account_snapshot()

    assert snapshot == {
        'connected': True,
        'reason': None,
        'login': 123456,
        'server': 'Demo-Server',
        'balance': 1000.0,
        'equity': 995.5,
        'free_margin': 980.25,
        'positions_total': 2,
    }
