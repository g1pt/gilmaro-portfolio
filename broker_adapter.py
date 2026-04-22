from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Protocol, cast, runtime_checkable

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    mt5 = None


LONG_SIDE = 'LONG'
SHORT_SIDE = 'SHORT'
FLAT_SIDE = 'FLAT'


def _format_mt5_error(error: Any) -> str:
    if isinstance(error, tuple) and len(error) >= 2:
        return f"{error[0]}:{error[1]}"
    return str(error)


def _account_snapshot_template(*, connected: bool, reason: str | None, login: int | None, server: str | None, balance: float | None, equity: float | None, free_margin: float | None, positions_total: int) -> dict[str, Any]:
    return {
        'connected': bool(connected),
        'reason': reason,
        'login': login,
        'server': server,
        'balance': balance,
        'equity': equity,
        'free_margin': free_margin,
        'positions_total': int(positions_total),
    }


def _decimal_from_number(value: float | str) -> Decimal:
    return Decimal(str(value))


def _pip_size_from_digits(digits: int) -> float:
    return 0.01 if digits in {2, 3} else 0.0001


def _fx_pair_components(symbol: str) -> tuple[str, str] | None:
    normalized = ''.join(ch for ch in str(symbol).upper() if ch.isalpha())
    if len(normalized) < 6:
        return None
    return normalized[:3], normalized[3:6]


def _strict_price_tolerance(tick_size: float) -> float:
    return max(float(tick_size) * 0.5, 1e-9)


def _coerce_int(value: Any, *, min_value: int | None = None) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if min_value is not None and parsed < min_value:
        return None
    return parsed


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _tick_timestamp_seconds(tick: Any) -> float | None:
    time_msc = getattr(tick, 'time_msc', None)
    if time_msc is not None:
        try:
            parsed_time_msc = float(time_msc)
        except (TypeError, ValueError):
            return None
        if parsed_time_msc > 0:
            return parsed_time_msc / 1000.0
    tick_time = getattr(tick, 'time', None)
    if tick_time is not None:
        try:
            parsed_tick_time = float(tick_time)
        except (TypeError, ValueError):
            return None
        if parsed_tick_time > 0:
            return parsed_tick_time
    return None


def _extract_account_state(account_info: Any, *, allow_missing_metrics: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    if account_info is None:
        return None, 'account_info_unavailable'

    login = _coerce_int(getattr(account_info, 'login', None), min_value=1)
    if login is None:
        return None, 'invalid_account_info:login'

    balance_raw = getattr(account_info, 'balance', None)
    balance = 0.0 if allow_missing_metrics and balance_raw is None else _coerce_float(balance_raw)
    if balance is None:
        return None, 'invalid_account_info:balance'

    equity_raw = getattr(account_info, 'equity', None)
    equity = 0.0 if allow_missing_metrics and equity_raw is None else _coerce_float(equity_raw)
    if equity is None:
        return None, 'invalid_account_info:equity'

    free_margin_raw = getattr(account_info, 'margin_free', None)
    free_margin = 0.0 if allow_missing_metrics and free_margin_raw is None else _coerce_float(free_margin_raw)
    if free_margin is None:
        return None, 'invalid_account_info:margin_free'

    leverage_raw = getattr(account_info, 'leverage', None)
    if leverage_raw is not None and _coerce_int(leverage_raw, min_value=1) is None:
        return None, 'invalid_account_info:leverage'

    trade_mode_raw = getattr(account_info, 'trade_mode', None)
    if trade_mode_raw is not None and not isinstance(trade_mode_raw, int):
        return None, 'invalid_account_info:trade_mode'

    return {
        'login': login,
        'server': str(getattr(account_info, 'server', '') or ''),
        'balance': balance,
        'equity': equity,
        'free_margin': free_margin,
    }, None


def _extract_result_retcode(result: Any) -> int | None:
    return _coerce_int(getattr(result, 'retcode', None))


def _extract_order_identifier(result: Any) -> str | None:
    order_or_deal = getattr(result, 'order', None)
    if order_or_deal in (None, '', 0, '0'):
        order_or_deal = getattr(result, 'deal', None)
    identifier = _coerce_int(order_or_deal, min_value=1)
    if identifier is None:
        return None
    return str(identifier)


@dataclass(frozen=True)
class SymbolSpecs:
    symbol: str
    category: str
    qty_step: float
    min_qty: float
    tick_size: float
    digits: int = 0
    point: float = 0.0
    pip_size: float = 0.0
    contract_size: float = 0.0
    lot_step: float = 0.0
    min_lot: float = 0.0
    max_lot: float | None = None
    pip_value_per_lot: float | None = None
    trade_mode: str | None = None
    account_currency: str = 'USD'

    @property
    def size_step(self) -> float:
        return self.lot_step if self.lot_step > 0 else self.qty_step

    @property
    def minimum_size(self) -> float:
        return self.min_lot if self.min_lot > 0 else self.min_qty


@dataclass
class BrokerOrderResult:
    success: bool
    order_id: str | None
    side: str
    qty: float
    avg_price: float | None
    raw_response: dict[str, Any]
    reason: str | None = None


@dataclass
class NormalizedPosition:
    symbol: str
    side: str
    size: float
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    broker_id: str | None = None
    position_idx: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def qty(self) -> float:
        return self.size

    @property
    def is_open(self) -> bool:
        return self.size > 0.0 and self.side in {LONG_SIDE, SHORT_SIDE}


@runtime_checkable
class BrokerAdapter(Protocol):
    def connect(self) -> bool: ...
    def get_symbol_specs(self, symbol: str) -> SymbolSpecs: ...
    def fetch_current_tick(self, symbol: str) -> dict[str, float]: ...
    def fetch_open_position(self, symbol: str) -> NormalizedPosition: ...
    def fetch_open_positions(self, symbol: str | None = None) -> list[NormalizedPosition]: ...
    def place_market_order(self, symbol: str, side: str, qty: float, **kwargs: Any) -> BrokerOrderResult: ...
    def close_position(self, symbol: str, side: str, qty: float, **kwargs: Any) -> BrokerOrderResult: ...
    def set_protection(self, symbol: str, side: str, take_profit_price: float, stop_loss_price: float, **kwargs: Any) -> bool: ...
    def verify_protection(self, symbol: str, expected_tp: float, expected_sl: float, tick_size: float) -> tuple[bool, NormalizedPosition]: ...
    def sync_position_state(self, symbol: str) -> NormalizedPosition: ...


class MT5BrokerAdapter:
    def __init__(self) -> None:
        login_raw = str(os.getenv('MT5_LOGIN', '')).strip()
        if login_raw:
            if not login_raw.isdigit():
                raise ValueError(f'INVALID MT5_LOGIN (not numeric): {login_raw}')
            self.login = int(login_raw)
            if self.login <= 0:
                raise ValueError(f'INVALID MT5_LOGIN (must be > 0): {login_raw}')

            # ONLY REQUIRED IF LOGIN IS USED
            self.password = str(os.getenv('MT5_PASSWORD', '')).strip()
            self.server = str(os.getenv('MT5_SERVER', '')).strip()

            if not self.password:
                raise ValueError('MT5_PASSWORD missing')
            if not self.server:
                raise ValueError('MT5_SERVER missing')
        else:
            self.login = 0  # USE ACTIVE MT5 SESSION
            self.password = ''
            self.server = ''
        self.path = str(os.getenv('MT5_PATH', '')).strip()
        self.timeout_ms = int(os.getenv('MT5_TIMEOUT_MS', '60000') or 60000)
        self.connected = False
        self.last_error: str | None = None
        self.filling_mode_cache: dict[str, int] = {}
        self.execution_fail_cache: dict[str, dict[str, float]] = {}

    def _mark_disconnected(self, reason: str | None) -> None:
        self.connected = False
        self.last_error = reason

    # =========================
    # ENV SAFE HELPERS
    # =========================
    def _env_bool(self, key: str, default: bool) -> bool:
        val = str(os.getenv(key, str(default))).lower()
        return val in ('1', 'true', 'yes')

    def _env_float(self, key: str, default: float) -> float:
        try:
            return float(os.getenv(key, str(default)) or default)
        except Exception:
            return default

    # =========================
    # CONTRACT SIZE FIX
    # =========================
    def _resolve_contract_size(self, symbol_info: Any) -> float:
        forced = self._env_float('FORCE_CONTRACT_SIZE', 0.0)
        if forced > 0:
            logging.warning('CONTRACT SIZE OVERRIDE | forced=%.2f (env)', forced)
            return forced
        return float(getattr(symbol_info, 'trade_contract_size', 100000) or 100000)

    # =========================
    # HARD LOT CAP (CRITICAL)
    # =========================
    def _apply_lot_caps(self, qty: float) -> float:
        max_lot = self._env_float('MAX_LIVE_FX_LOT', 0.01)
        min_lot = self._env_float('MIN_POSITION_SIZE', 0.01)
        qty = min(qty, max_lot)
        qty = max(qty, min_lot)
        return qty

    def _resolve_mt5_path(self) -> str | None:
        path = (self.path or '').strip()
        if not path or path.lower() in {'auto', 'none'}:
            logging.warning('MT5 PATH RESOLVER | auto mode → using active terminal')
            return None
        if not os.path.exists(path):
            logging.warning('MT5 PATH RESOLVER | invalid path=%s → fallback to active terminal', path)
            return None
        logging.info('MT5 PATH RESOLVER | using explicit path=%s', path)
        return path

    def connect(self) -> bool:
        if mt5 is None:
            self._mark_disconnected('package_unavailable')
            logging.critical('MT5 CONNECT FAILURE | reason=package_unavailable')
            return False
        resolved_path = self._resolve_mt5_path()
        for attempt in range(1, 4):
            logging.info('MT5 CONNECT ATTEMPT | attempt=%s/3 login=%s server=%s path=%s', attempt, self.login or 'unset', self.server or 'unset', resolved_path)
            self._mark_disconnected(None)
            try:
                mt5.shutdown()
            except Exception as exc:
                logging.warning('MT5 SHUTDOWN WARNING | attempt=%s error=%s', attempt, exc)

            if resolved_path is not None:
                initialized = mt5.initialize(path=resolved_path, timeout=180000)
            else:
                initialized = mt5.initialize(timeout=180000)
            if not initialized:
                error = mt5.last_error()
                self._mark_disconnected(str(error))
                logging.critical('MT5 CONNECT FAILURE | reason=initialize_failed error=%s path=%s', _format_mt5_error(error), resolved_path)
                if attempt < 3:
                    logging.warning('MT5 RETRY | attempt=%s/3 error=%s', attempt, _format_mt5_error(error))
                    time.sleep(2)
                continue

            account_info = mt5.account_info()
            if account_info is None:
                error = mt5.last_error()
                self._mark_disconnected(_format_mt5_error(error))
                logging.critical('MT5 SESSION VALIDATION | status=failed reason=account_info_none error=%s', self.last_error)
                try:
                    mt5.shutdown()
                except Exception as exc:
                    logging.warning('MT5 SHUTDOWN WARNING | attempt=%s validation_error=%s', attempt, exc)
                if attempt < 3:
                    time.sleep(2)
                continue

            account_login = _coerce_int(getattr(account_info, 'login', None), min_value=0)
            if account_login is None:
                self._mark_disconnected('invalid_account_info:login')
                logging.critical('MT5 SESSION VALIDATION | status=failed reason=invalid_account_info:login')
                try:
                    mt5.shutdown()
                except Exception as exc:
                    logging.warning('MT5 SHUTDOWN WARNING | attempt=%s validation_error=%s', attempt, exc)
                if attempt < 3:
                    time.sleep(2)
                    continue
                return False

            account_login = int(account_login)
            account_server = str(getattr(account_info, 'server', '') or '')
            logging.info('MT5 SESSION VALIDATION | status=checking expected_login=%s actual_login=%s expected_server=%s actual_server=%s', self.login or 'unset', account_login or 'unset', self.server or 'unset', account_server or 'unset')
            if account_login == 0:
                logging.warning(
                    'MT5 SESSION WARNING | login=0 detected → retrying instead of failing'
                )
                try:
                    mt5.shutdown()
                except Exception:
                    pass
                if attempt < 3:
                    time.sleep(2)
                    continue
                else:
                    logging.error('MT5 CONNECT FAILURE | invalid account after retries')
                    self._mark_disconnected('invalid_account')
                    return False
            if self.login and account_login != self.login:
                logging.warning('MT5 SESSION WARNING | login mismatch but allowed | expected=%s actual=%s', self.login, account_login)
            if self.server and account_server and self.server != account_server:
                logging.warning('MT5 SESSION WARNING | server mismatch but allowed | expected=%s actual=%s', self.server, account_server)

            self.connected = True
            self.last_error = None
            logging.info('MT5 CONNECTED | login=%s balance=%.2f server=%s', account_login, float(_coerce_float(getattr(account_info, 'balance', None)) or 0.0), account_server)
            return True
        self._mark_disconnected(self.last_error or 'unknown')
        logging.critical('MT5 CONNECT FAILURE | attempts=3 error=%s path=%s', self.last_error or 'unknown', resolved_path)
        return False

    def get_mt5_account_snapshot(self) -> dict[str, Any]:
        def _disconnected_snapshot(reason: str | None) -> dict[str, Any]:
            self._mark_disconnected(reason)
            snapshot = _account_snapshot_template(
                connected=False,
                reason=self.last_error,
                login=None,
                server=None,
                balance=None,
                equity=None,
                free_margin=None,
                positions_total=0,
            )
            snapshot.pop('free_margin', None)
            return snapshot

        if mt5 is None:
            return _account_snapshot_template(
                connected=False,
                reason=self.last_error or 'package_unavailable',
                login=None,
                server=None,
                balance=None,
                equity=None,
                free_margin=None,
                positions_total=0,
            )
        if not self.connected:
            current_error = mt5.last_error()
            return _disconnected_snapshot(self.last_error or _format_mt5_error(current_error))

        account_info = mt5.account_info()
        account_state, account_error = _extract_account_state(account_info)
        if account_state is None:
            if account_error == 'account_info_unavailable':
                error = mt5.last_error()
                return _disconnected_snapshot(_format_mt5_error(error))
            return _disconnected_snapshot(account_error)

        try:
            positions_total_raw = mt5.positions_total()
        except Exception:
            positions_total_raw = None
        if positions_total_raw is None:
            error = mt5.last_error()
            return _disconnected_snapshot(_format_mt5_error(error))

        try:
            positions_total = int(cast(int, positions_total_raw))
        except (TypeError, ValueError):
            return _disconnected_snapshot(f'invalid_positions_total:{positions_total_raw}')
        if positions_total < 0:
            return _disconnected_snapshot(f'invalid_positions_total:{positions_total}')

        self.connected = True
        self.last_error = None
        return _account_snapshot_template(
            connected=True,
            reason=None,
            login=int(account_state['login']),
            server=str(account_state['server']),
            balance=float(account_state['balance']),
            equity=float(account_state['equity']),
            free_margin=float(account_state['free_margin']),
            positions_total=positions_total,
        )

    def get_symbol_specs(self, symbol: str) -> SymbolSpecs:
        self._ensure_connected()
        info = mt5.symbol_info(symbol)
        if info is None:
            error = f'symbol_info_unavailable:{symbol}'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=symbol_info_none symbol=%s error=%s', symbol, error)
            raise RuntimeError(f'MT5 symbol_info unavailable for {symbol}')
        if not info.visible:
            logging.info('MT5 SYMBOL VISIBILITY | symbol=%s visible=false action=select', symbol)
            if not mt5.symbol_select(symbol, True):
                error = f'symbol_select_failed:{symbol}'
                self._mark_disconnected(error)
                logging.critical('MT5 DATA CHANNEL FAILURE | reason=symbol_select_failed symbol=%s error=%s', symbol, error)
                raise RuntimeError(f'MT5 symbol_select failed for {symbol}')
            info = mt5.symbol_info(symbol)
            if info is None:
                error = f'symbol_info_unavailable_after_select:{symbol}'
                self._mark_disconnected(error)
                logging.critical('MT5 DATA CHANNEL FAILURE | reason=symbol_info_none_after_select symbol=%s error=%s', symbol, error)
                raise RuntimeError(f'MT5 symbol_info unavailable after select for {symbol}')
        point = float(info.point or 0.0)
        digits = int(info.digits or 0)
        trade_mode = str(getattr(info, 'trade_mode', ''))
        tick_value = float(getattr(info, 'trade_tick_value', 0.0) or 0.0)
        pip_value_per_lot = None
        if tick_value > 0 and point > 0:
            pip_value_per_lot = tick_value * (_pip_size_from_digits(digits) / point)
        specs = normalize_mt5_symbol_info(symbol, info)
        specs = SymbolSpecs(**{**specs.__dict__, 'trade_mode': trade_mode, 'pip_value_per_lot': pip_value_per_lot})
        logging.info('MT5 SYMBOL READY | symbol=%s digits=%d point=%.10f lot_step=%.4f min_lot=%.4f filling_mode=%s', symbol, digits, point, specs.lot_step, specs.min_lot, getattr(info, 'filling_mode', 'unknown'))
        return specs

    def fetch_current_tick(self, symbol: str) -> dict[str, float]:
        self._ensure_connected()
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            error = f'symbol_info_unavailable:{symbol}'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=symbol_info_none symbol=%s error=%s', symbol, error)
            raise RuntimeError(f'MT5 symbol_info unavailable for {symbol}')
        if not bool(getattr(symbol_info, 'visible', False)):
            logging.info('MT5 SYMBOL VISIBILITY | symbol=%s visible=false action=select', symbol)
            if not mt5.symbol_select(symbol, True):
                error = f'symbol_select_failed:{symbol}'
                self._mark_disconnected(error)
                logging.critical('MT5 DATA CHANNEL FAILURE | reason=symbol_select_failed symbol=%s error=%s', symbol, error)
                raise RuntimeError(f'MT5 symbol_select failed for {symbol}')
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info is None:
                error = f'symbol_info_unavailable_after_select:{symbol}'
                self._mark_disconnected(error)
                logging.critical('MT5 DATA CHANNEL FAILURE | reason=symbol_info_none_after_select symbol=%s error=%s', symbol, error)
                raise RuntimeError(f'MT5 symbol_info unavailable after select for {symbol}')
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            error = f'symbol_info_tick_unavailable:{symbol}'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=symbol_info_tick_none symbol=%s error=%s', symbol, error)
            raise RuntimeError(f'MT5 symbol_info_tick unavailable for {symbol}')
        bid = float(getattr(tick, 'bid', 0.0) or 0.0)
        ask = float(getattr(tick, 'ask', 0.0) or 0.0)
        last = float(getattr(tick, 'last', 0.0) or 0.0)
        if bid <= 0.0 and ask <= 0.0 and last <= 0.0:
            error = f'invalid_tick_values:{symbol}'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=invalid_tick_values symbol=%s error=%s', symbol, error)
            raise RuntimeError(f'MT5 invalid tick values for {symbol}')
        tick_timestamp = _tick_timestamp_seconds(tick)
        max_tick_age_seconds = max(self._env_float('MT5_MAX_TICK_AGE_SECONDS', 30.0), 0.0)
        if tick_timestamp is not None and max_tick_age_seconds > 0.0:
            tick_age_seconds = time.time() - tick_timestamp
            if not math.isfinite(tick_age_seconds) or tick_age_seconds > max_tick_age_seconds:
                error = f'stale_tick:{symbol}'
                self._mark_disconnected(error)
                logging.critical(
                    'MT5 DATA CHANNEL FAILURE | reason=stale_tick symbol=%s error=%s age_seconds=%.3f max_age_seconds=%.3f',
                    symbol,
                    error,
                    tick_age_seconds,
                    max_tick_age_seconds,
                )
                raise RuntimeError(f'MT5 stale tick for {symbol}')
        return {'bid': bid, 'ask': ask, 'last': last}

    def fetch_open_positions(self, symbol: str | None = None) -> list[NormalizedPosition]:
        self._ensure_connected()
        rows = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if rows is None:
            error = _format_mt5_error(mt5.last_error())
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=positions_get_none symbol=%s error=%s', symbol or 'all', error)
            raise RuntimeError(f'MT5 positions_get failed: {mt5.last_error()}')
        try:
            return [self._normalize_position(row) for row in rows]
        except Exception as exc:
            error = f'positions_get_malformed:{exc}'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=positions_get_malformed symbol=%s error=%s', symbol or 'all', exc)
            raise RuntimeError(f'MT5 positions_get malformed: {exc}') from exc

    def get_open_orders(self, symbol: str | None = None) -> list[Any]:
        self._ensure_connected()
        orders_get = getattr(mt5, 'orders_get', None)
        if not callable(orders_get):
            error = 'orders_get_unavailable'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=orders_get_unavailable symbol=%s error=%s', symbol or 'all', error)
            raise RuntimeError('MT5 orders_get unavailable')
        try:
            orders = orders_get()
        except Exception as exc:
            error = f'orders_get_exception:{exc}'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=orders_get_exception symbol=%s error=%s', symbol or 'all', exc)
            raise RuntimeError(f'MT5 orders_get failed: {exc}') from exc
        if orders is None:
            last_error_getter = getattr(mt5, 'last_error', None)
            error = _format_mt5_error(last_error_getter()) if callable(last_error_getter) else 'orders_get_none'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=orders_get_none symbol=%s error=%s', symbol or 'all', error)
            raise RuntimeError(f'MT5 orders_get failed: {error}')
        try:
            order_list = list(orders)
            if symbol:
                return [order for order in order_list if getattr(order, 'symbol', None) == symbol]
            return order_list
        except Exception as exc:
            error = f'orders_get_malformed:{exc}'
            self._mark_disconnected(error)
            logging.critical('MT5 DATA CHANNEL FAILURE | reason=orders_get_malformed symbol=%s error=%s', symbol or 'all', exc)
            raise RuntimeError(f'MT5 orders_get malformed: {exc}') from exc

    def fetch_open_position(self, symbol: str) -> NormalizedPosition:
        positions = self.fetch_open_positions(symbol)
        for position in positions:
            if position.symbol == symbol and position.is_open:
                return position
        return NormalizedPosition(symbol=symbol, side=FLAT_SIDE, size=0.0, entry_price=0.0, raw={})

    # =========================
    # 🔥 UNIVERSAL FX POSITION SIZER (FINAL)
    # =========================
    def _calculate_safe_lot(self, symbol: str, symbol_info: Any, equity: float) -> float:
        try:
            contract_size = self._resolve_contract_size(symbol_info)

            digits = int(getattr(symbol_info, 'digits', 5) or 5)
            point = float(getattr(symbol_info, 'point', 0.00001) or 0.00001)

            # pip model (ALL FX SAFE)
            if digits in (5, 3):
                pip_size = point * 10.0
            elif digits in (4, 2):
                pip_size = point
            else:
                pip_size = point

            pip_value_per_lot = contract_size * pip_size

            if pip_value_per_lot <= 0 or pip_value_per_lot > 1_000_000:
                logging.warning(
                    'PIP FALLBACK | symbol=%s raw=%.5f → using 10.0',
                    symbol,
                    pip_value_per_lot,
                )
                pip_value_per_lot = 10.0

            risk_pct = float(os.getenv('RISK_PER_TRADE', '0.005'))
            stop_pips = float(os.getenv('DEFAULT_STOP_PIPS', '10.0'))

            risk_amount = equity * risk_pct
            lot = risk_amount / (pip_value_per_lot * stop_pips)

            logging.warning(
                'SIZING | symbol=%s equity=%.2f contract=%.2f risk=%.4f stop=%.2f pip=%.5f lot=%.5f',
                symbol,
                equity,
                contract_size,
                risk_pct,
                stop_pips,
                pip_value_per_lot,
                lot,
            )

            return float(lot)

        except Exception as exc:
            logging.error('SIZING FAILED | %s', str(exc))
            return 0.01

    # =========================
    # 🔥 SAFE MARGIN CHECK (FINAL)
    # =========================
    def _estimate_margin(self, symbol: str, qty: float, price: float, symbol_info: Any) -> float:
        try:
            leverage = float(os.getenv('DEFAULT_LEVERAGE', '100') or 100)
            if leverage <= 0:
                leverage = 100.0
            contract_size = self._resolve_contract_size(symbol_info)

            # correct FX detection
            is_fx = len(symbol) == 6 and symbol[:3].isalpha() and symbol[3:].isalpha()

            if is_fx:
                notional = qty * contract_size
            else:
                notional = qty * contract_size * price

            margin = notional / leverage

            if not math.isfinite(margin) or margin <= 0:
                return 999999.0
            return float(margin)
        except Exception as exc:
            logging.error('MARGIN ERROR | %s', str(exc))
            return 999999.0

    def place_market_order(self, symbol: str, side: str, qty: float, **kwargs: Any) -> BrokerOrderResult:
        try:
            self.validate_order_channel()
        except RuntimeError:
            account_state, account_error = _extract_account_state(
                mt5.account_info() if mt5 is not None else None,
                allow_missing_metrics=True,
            )
            if account_state is None:
                failure_reason = 'account_info_unavailable' if account_error == 'account_info_unavailable' else str(account_error).replace(':', '_')
                logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=%s', symbol, side, qty, failure_reason)
                return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, failure_reason)
            raise
        self.execution_fail_cache = getattr(self, 'execution_fail_cache', {})
        key = f'{symbol}_{side}'
        now = time.time()
        entry = self.execution_fail_cache.get(key, {'fails': 0, 'last_fail': 0.0})
        fails = int(entry.get('fails', 0))
        last_fail = float(entry.get('last_fail', 0.0))
        if fails >= 3:
            if now - last_fail < 60.0:
                logging.warning(
                    'EXECUTION BLOCKED | symbol=%s side=%s cooldown_active',
                    symbol,
                    side,
                )
                return BrokerOrderResult(
                    False,
                    None,
                    side,
                    float(qty),
                    None,
                    {'symbol': symbol},
                    'cooldown',
                )
            entry = {'fails': 0, 'last_fail': 0.0}
            fails = 0
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=symbol_info_unavailable', symbol, side, qty)
            return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, 'symbol_info_unavailable')
        symbol_visible = getattr(symbol_info, 'visible', None)
        if symbol_visible is False:
            logging.info('MT5 SYMBOL VISIBILITY | symbol=%s visible=false action=select', symbol)
            symbol_select = getattr(mt5, 'symbol_select', None)
            if not callable(symbol_select):
                self._mark_disconnected(f'symbol_select_unavailable:{symbol}')
                logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=symbol_select_unavailable', symbol, side, qty)
                return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, 'symbol_select_unavailable')
            elif not symbol_select(symbol, True):
                self._mark_disconnected(f'symbol_select_failed:{symbol}')
                logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=symbol_select_failed', symbol, side, qty)
                return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, 'symbol_select_failed')
            else:
                symbol_info = mt5.symbol_info(symbol)
                if symbol_info is None:
                    self._mark_disconnected(f'symbol_info_unavailable_after_select:{symbol}')
                    logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=symbol_info_unavailable_after_select', symbol, side, qty)
                    return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, 'symbol_info_unavailable_after_select')
                symbol_visible = getattr(symbol_info, 'visible', None)
                if symbol_visible is False:
                    self._mark_disconnected(f'symbol_not_visible_after_select:{symbol}')
                    logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=symbol_not_visible_after_select', symbol, side, qty)
                    return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, 'symbol_not_visible_after_select')
        tradability_reason = self._validate_symbol_tradability(symbol=symbol, side=side, symbol_info=symbol_info)
        if tradability_reason is not None:
            self._mark_disconnected(f'{tradability_reason}:{symbol}')
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=%s', symbol, side, qty, tradability_reason)
            return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, tradability_reason)
        # =========================
        # FETCH PRICE
        # =========================
        order_type = mt5.ORDER_TYPE_BUY if side == LONG_SIDE else mt5.ORDER_TYPE_SELL
        try:
            tick = self.fetch_current_tick(symbol)
        except Exception as exc:
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=tick_fetch_failed err=%s', symbol, side, qty, str(exc))
            return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, 'tick_fetch_failed')
        live_price = tick['ask'] if side == LONG_SIDE else tick['bid']
        if live_price <= 0:
            live_price = tick['last']
        if live_price <= 0:
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=invalid_live_price', symbol, side, qty)
            return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, 'invalid_live_price')

        # =========================
        # ACCOUNT INFO
        # =========================
        acc = mt5.account_info()
        account_state, account_error = _extract_account_state(acc, allow_missing_metrics=True)
        if account_state is None:
            self._mark_disconnected(account_error)
            failure_reason = 'account_info_unavailable' if account_error == 'account_info_unavailable' else str(account_error).replace(':', '_')
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=%s', symbol, side, qty, failure_reason)
            return BrokerOrderResult(False, None, side, float(qty), None, {'symbol': symbol, 'requested_qty': qty}, failure_reason)
        equity = float(account_state['equity'])
        free_margin = float(account_state['free_margin'])
        balance = float(account_state['balance'])
        effective_equity = equity if equity > 0 else free_margin if free_margin > 0 else balance if balance > 0 else 0.0
        margin_available = free_margin if free_margin > 0 else effective_equity

        # =========================
        # 🔥 NEW SIZING ENGINE
        # =========================
        safe_lot = self._calculate_safe_lot(symbol, symbol_info, effective_equity)

        # SAFETY CLAMP
        max_lot_env = float(os.getenv('MAX_LIVE_FX_LOT', '0.02'))
        if safe_lot > max_lot_env * 5:
            logging.warning('LOT HARD CLAMP | %.5f → %.5f', safe_lot, max_lot_env * 5)
            safe_lot = max_lot_env * 5

        # fallback
        if not math.isfinite(safe_lot) or safe_lot <= 0:
            safe_lot = float(os.getenv('MIN_POSITION_SIZE', '0.01'))

        # apply caps
        capped_qty = self._apply_lot_caps(safe_lot)
        final_qty = self._normalize_volume(symbol=symbol, qty=capped_qty, symbol_info=symbol_info)

        if final_qty <= 0:
            logging.error('MT5 ORDER FAILED | normalized_volume_zero')
            return BrokerOrderResult(False, None, side, float(qty), None, {}, 'normalized_volume_zero')

        # =========================
        # 🔥 MARGIN CHECK
        # =========================
        estimated_margin = self._estimate_margin(symbol, final_qty, live_price, symbol_info)
        logging.warning(
            'MARGIN CHECK | symbol=%s qty=%.4f margin=%.2f equity=%.2f',
            symbol,
            final_qty,
            estimated_margin,
            margin_available,
        )
        if margin_available > 0 and estimated_margin > margin_available * 0.9:
            logging.warning('BLOCKED | insufficient margin')
            return BrokerOrderResult(
                False,
                None,
                side,
                float(final_qty),
                None,
                {'margin': estimated_margin, 'equity': margin_available},
                'insufficient_margin',
            )
        logging.warning(
            'POSITION FIX | equity=%.2f raw=%.5f capped=%.5f final=%.5f',
            margin_available,
            safe_lot,
            capped_qty,
            final_qty,
        )
        requested_filling_mode = kwargs.get('type_filling')
        request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': symbol,
            'volume': float(final_qty),
            'type': order_type,
            'price': float(kwargs.get('price') or live_price),
            'deviation': int(kwargs.get('deviation', 20)),
            'type_time': mt5.ORDER_TIME_GTC,
            'comment': kwargs.get('comment', 'hftbot'),
        }
        if kwargs.get('position') is not None:
            request['position'] = int(kwargs['position'])
        if kwargs.get('sl') is not None:
            request['sl'] = float(kwargs['sl'])
        if kwargs.get('tp') is not None:
            request['tp'] = float(kwargs['tp'])
        logging.info(
            'MT5 ORDER PREP | symbol=%s side=%s requested_qty=%.12f normalized_qty=%.12f price=%.10f visible=%s',
            symbol,
            side,
            float(qty),
            float(final_qty),
            float(request['price']),
            str(bool(symbol_visible) if symbol_visible is not None else True).lower(),
        )
        result, used_filling_mode = self.send_order_with_fallback(
            request=request,
            symbol=symbol,
            symbol_info=symbol_info,
            requested_mode=int(requested_filling_mode) if requested_filling_mode is not None else None,
        )
        if result is None:
            self.execution_fail_cache[key] = {'fails': fails + 1, 'last_fail': now}
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=result_none', symbol, side, qty)
            return BrokerOrderResult(False, None, side, float(final_qty), None, {'symbol': symbol, 'requested_qty': qty, 'normalized_qty': final_qty, 'used_filling_mode': used_filling_mode}, 'result_none')
        logging.info('ORDER RESULT | retcode=%s comment=%s', str(getattr(result, 'retcode', '')), str(getattr(result, 'comment', '')))
        retcode = _extract_result_retcode(result)
        if retcode is None:
            self.execution_fail_cache[key] = {'fails': fails + 1, 'last_fail': now}
            self._mark_disconnected(f'invalid_retcode:{symbol}')
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=invalid_retcode filling=%s', symbol, side, float(final_qty), str(used_filling_mode))
            return BrokerOrderResult(
                False,
                None,
                side,
                float(final_qty),
                None,
                {'symbol': symbol, 'requested_qty': qty, 'normalized_qty': final_qty, 'comment': str(getattr(result, 'comment', '')), 'used_filling_mode': used_filling_mode},
                'invalid_retcode',
            )
        if retcode != mt5.TRADE_RETCODE_DONE:
            self.execution_fail_cache[key] = {'fails': fails + 1, 'last_fail': now}
        else:
            self.execution_fail_cache[key] = {'fails': 0, 'last_fail': 0.0}
        if retcode != mt5.TRADE_RETCODE_DONE:
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f retcode=%s comment=%s filling=%s', symbol, side, float(final_qty), str(retcode), str(getattr(result, 'comment', '')), str(used_filling_mode))
            return BrokerOrderResult(
                False,
                None,
                side,
                float(final_qty),
                None,
                {'symbol': symbol, 'requested_qty': qty, 'normalized_qty': final_qty, 'retcode': retcode, 'comment': str(getattr(result, 'comment', '')), 'used_filling_mode': used_filling_mode},
                str(getattr(result, 'comment', '')) or f'retcode_{retcode}',
            )
        if used_filling_mode is not None:
            self.filling_mode_cache[symbol] = int(used_filling_mode)
        order_id = _extract_order_identifier(result)
        if order_id is None:
            self.execution_fail_cache[key] = {'fails': fails + 1, 'last_fail': now}
            self._mark_disconnected(f'invalid_order_id:{symbol}')
            logging.error('MT5 ORDER FAILED | symbol=%s side=%s qty=%.12f reason=invalid_order_id filling=%s', symbol, side, float(final_qty), str(used_filling_mode))
            return BrokerOrderResult(
                False,
                None,
                side,
                float(final_qty),
                None,
                {'symbol': symbol, 'requested_qty': qty, 'normalized_qty': final_qty, 'retcode': retcode, 'comment': str(getattr(result, 'comment', '')), 'used_filling_mode': used_filling_mode},
                'invalid_order_id',
            )
        fill_price = float(getattr(result, 'price', 0.0) or request['price'])
        logging.info('MT5 ORDER SUCCESS | symbol=%s side=%s qty=%.12f order_id=%s fill_price=%.10f filling=%s', symbol, side, float(final_qty), str(order_id), float(fill_price), str(used_filling_mode))
        return BrokerOrderResult(
            True,
            str(order_id),
            side,
            float(final_qty),
            float(fill_price),
            {'symbol': symbol, 'requested_qty': qty, 'normalized_qty': final_qty, 'used_filling_mode': used_filling_mode, 'retcode': retcode},
            None,
        )

    def close_position(self, symbol: str, side: str, qty: float, **kwargs: Any) -> BrokerOrderResult:
        open_position = self.fetch_open_position(symbol)
        if not open_position.is_open:
            logging.info('MT5 CLOSE CONFIRMED | symbol=%s reason=already_flat', symbol)
            return BrokerOrderResult(True, None, str(side).upper(), float(qty), None, {'symbol': symbol, 'status': 'already_flat'}, None)
        position = kwargs.get('position') or open_position.broker_id
        if position is None:
            logging.error('MT5 CLOSE FAILED | symbol=%s reason=missing_position_ticket', symbol)
            return BrokerOrderResult(False, None, side, qty, None, {'symbol': symbol}, 'missing_position_ticket')
        requested_qty = float(qty)
        safe_qty = min(requested_qty, float(open_position.size))
        request, close_side = self.build_mt5_close_request(
            symbol=symbol,
            position_side=open_position.side,
            qty=safe_qty,
            position_ticket=position,
            comment=kwargs.get('comment', 'hftbot-close'),
            requested_filling_mode=kwargs.get('type_filling'),
            deviation=kwargs.get('deviation'),
        )
        if request is None:
            return BrokerOrderResult(False, None, str(side).upper(), requested_qty, None, {'symbol': symbol, 'requested_qty': requested_qty}, 'close_build_failed')
        logging.info('MT5 CLOSE ATTEMPT | symbol=%s side=%s qty=%.12f position=%s', symbol, close_side, request['volume'], request['position'])
        symbol_info = mt5.symbol_info(symbol)
        result, used_mode = self.send_order_with_fallback(
            request=request,
            symbol=symbol,
            symbol_info=symbol_info,
            requested_mode=int(kwargs['type_filling']) if kwargs.get('type_filling') is not None else None,
            refresh_request=self._build_tick_refresher(side=close_side),
        )
        raw: dict[str, Any] = {'request': dict(request), 'used_filling_mode': used_mode}
        retcode: int | None = None
        order_id: str | None = None
        avg_price: float | None = None
        response_issue: str | None = None
        if result is None:
            response_issue = 'order_send_none'
        else:
            retcode = _extract_result_retcode(result)
            if hasattr(result, '_asdict'):
                try:
                    raw_candidate = result._asdict()
                except Exception:
                    raw_candidate = None
                if isinstance(raw_candidate, dict):
                    raw.update(raw_candidate)
            raw.setdefault('retcode', getattr(result, 'retcode', None))
            raw.setdefault('comment', str(getattr(result, 'comment', '')))
            order_id = _extract_order_identifier(result)
            avg_price = _coerce_float(getattr(result, 'price', None))
            if retcode is None:
                response_issue = 'invalid_retcode'
            elif retcode == getattr(mt5, 'TRADE_RETCODE_DONE', 10009) and used_mode is not None:
                self.filling_mode_cache[symbol] = int(used_mode)
        remaining = self.fetch_open_position(symbol)
        logged_retcode = retcode if retcode is not None else raw.get('retcode')
        logging.info('MT5 CLOSE RECHECK | symbol=%s retcode=%s remaining_open=%s remaining_side=%s remaining_qty=%.12f', symbol, logged_retcode, str(remaining.is_open).lower(), remaining.side, remaining.size)
        if not remaining.is_open:
            logging.info('MT5 CLOSE CONFIRMED | symbol=%s order_id=%s', symbol, order_id or 'none')
            return BrokerOrderResult(True, order_id, close_side, float(request['volume']), avg_price, raw, None)
        if response_issue is not None:
            disconnect_reason = f'close_result_none:{symbol}' if response_issue == 'order_send_none' else f'invalid_close_retcode:{symbol}'
            self._mark_disconnected(disconnect_reason)
            logging.error('MT5 CLOSE FAILED FINAL | symbol=%s retcode=%s reason=%s', symbol, logged_retcode, response_issue)
            return BrokerOrderResult(False, order_id, close_side, float(request['volume']), avg_price, raw, response_issue)
        assert retcode is not None
        if remaining.side == open_position.side and remaining.size < open_position.size:
            raw['remaining_position'] = remaining.raw
            logging.warning('MT5 CLOSE PARTIAL | symbol=%s requested_qty=%.12f remaining_qty=%.12f', symbol, float(request['volume']), remaining.size)
            return BrokerOrderResult(False, order_id, close_side, float(request['volume']), avg_price, raw, 'close_partial_remaining')
        reason = str(getattr(result, 'comment', 'mt5_close_rejected'))
        raw['remaining_position'] = remaining.raw
        logging.error('MT5 CLOSE FAILED FINAL | symbol=%s retcode=%s reason=%s', symbol, retcode, reason)
        return BrokerOrderResult(False, order_id, close_side, float(request['volume']), avg_price, raw, reason)

    def build_mt5_close_request(self, *, symbol: str, position_side: str, qty: float, position_ticket: Any, comment: str, requested_filling_mode: Any = None, deviation: Any = None) -> tuple[dict[str, Any] | None, str]:
        normalized_position_side = str(position_side).upper()
        if normalized_position_side not in {LONG_SIDE, SHORT_SIDE}:
            logging.error('MT5 CLOSE BUILD | symbol=%s reason=invalid_position_side side=%s', symbol, position_side)
            return None, FLAT_SIDE
        close_side = LONG_SIDE if normalized_position_side == SHORT_SIDE else SHORT_SIDE
        symbol_info = mt5.symbol_info(symbol)
        normalized_qty = self._normalize_volume(symbol=symbol, qty=qty, symbol_info=symbol_info)
        if normalized_qty <= 0:
            logging.error('MT5 CLOSE BUILD | symbol=%s reason=normalized_volume_zero requested_qty=%.12f', symbol, qty)
            return None, close_side
        try:
            position_value = int(position_ticket)
        except (TypeError, ValueError):
            logging.error('MT5 CLOSE BUILD | symbol=%s reason=invalid_position_ticket position=%r', symbol, position_ticket)
            return None, close_side
        tick = self.fetch_current_tick(symbol)
        close_price = float(tick['ask'] if close_side == LONG_SIDE else tick['bid'])
        request: dict[str, Any] = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': symbol,
            'volume': float(normalized_qty),
            'type': mt5.ORDER_TYPE_BUY if close_side == LONG_SIDE else mt5.ORDER_TYPE_SELL,
            'price': close_price,
            'position': position_value,
            'deviation': int(deviation if deviation is not None else 20),
            'type_time': mt5.ORDER_TIME_GTC,
            'comment': comment,
        }
        if requested_filling_mode is not None:
            request['type_filling'] = int(requested_filling_mode)
        logging.info('MT5 CLOSE BUILD | symbol=%s position_side=%s close_side=%s qty=%.12f price=%.6f position=%s', symbol, normalized_position_side, close_side, normalized_qty, close_price, position_value)
        return request, close_side

    def _build_tick_refresher(self, *, side: str) -> Any:
        order_side = str(side).upper()

        def _refresh(request: dict[str, Any], attempt: int) -> None:
            tick = self.fetch_current_tick(str(request['symbol']))
            request['price'] = float(tick['ask'] if order_side == LONG_SIDE else tick['bid'])
            logging.info('MT5 CLOSE FALLBACK | symbol=%s attempt=%d refreshed_price=%.6f', request['symbol'], attempt, request['price'])

        return _refresh

    def _normalize_volume(self, *, symbol: str, qty: float, symbol_info: Any) -> float:
        raw_qty = float(qty or 0.0)
        step = float(getattr(symbol_info, 'volume_step', 0.0) or 0.0)
        min_volume = float(getattr(symbol_info, 'volume_min', 0.0) or 0.0)
        max_volume = float(getattr(symbol_info, 'volume_max', 0.0) or 0.0)
        normalized_qty = round_size_to_step(raw_qty, step if step > 0 else 0.0) if step > 0 else raw_qty
        if max_volume > 0:
            normalized_qty = min(normalized_qty, max_volume)
        if min_volume > 0 and normalized_qty < min_volume:
            return 0.0
        return max(0.0, normalized_qty)

    def set_protection(self, symbol: str, side: str, take_profit_price: float, stop_loss_price: float, **kwargs: Any) -> bool:
        self._ensure_connected()
        position = kwargs.get('position')
        if position is None:
            current = self.fetch_open_position(symbol)
            position = current.broker_id
        request = {
            'action': mt5.TRADE_ACTION_SLTP,
            'symbol': symbol,
            'position': int(position),
            'sl': float(stop_loss_price),
            'tp': float(take_profit_price),
        }
        logging.info('MT5 PROTECTION SUBMIT | symbol=%s side=%s position=%s tp=%.6f sl=%.6f', symbol, side, position, take_profit_price, stop_loss_price)
        result = mt5.order_send(request)
        if result is None:
            self._mark_disconnected(f'protection_result_none:{symbol}')
            logging.info('MT5 PROTECTION RESULT | symbol=%s success=false retcode=%s', symbol, None)
            return False
        retcode = _extract_result_retcode(result)
        if retcode is None:
            self._mark_disconnected(f'invalid_protection_retcode:{symbol}')
            logging.info('MT5 PROTECTION RESULT | symbol=%s success=false retcode=%s', symbol, getattr(result, 'retcode', None))
            return False
        success = retcode == getattr(mt5, 'TRADE_RETCODE_DONE', 10009)
        logging.info('MT5 PROTECTION RESULT | symbol=%s success=%s retcode=%s', symbol, str(success).lower(), retcode)
        return success

    def verify_protection(self, symbol: str, expected_tp: float, expected_sl: float, tick_size: float) -> tuple[bool, NormalizedPosition]:
        position = self.fetch_open_position(symbol)
        tolerance = _strict_price_tolerance(tick_size)
        ok = (
            position.is_open
            and position.take_profit is not None
            and position.stop_loss is not None
            and abs(float(position.take_profit) - float(expected_tp)) <= tolerance
            and abs(float(position.stop_loss) - float(expected_sl)) <= tolerance
        )
        return ok, position

    def _resolve_filling_mode(self, symbol: str) -> int:
        cached_mode = self.filling_mode_cache.get(symbol)
        if cached_mode is not None:
            logging.info('MT5 FILLING MODE | symbol=%s selected=%s source=cache', symbol, cached_mode)
            return int(cached_mode)
        info = mt5.symbol_info(symbol)
        if info is None:
            logging.warning('MT5 FILLING MODE | symbol=%s reason=symbol_info_unavailable fallback=%s', symbol, getattr(mt5, 'ORDER_FILLING_IOC', 1))
            return getattr(mt5, 'ORDER_FILLING_IOC', 1)
        supported = self._supported_filling_modes(info)
        if not supported:
            filling_mode = getattr(info, 'filling_mode', None)
            if filling_mode in {getattr(mt5, 'ORDER_FILLING_FOK', None), getattr(mt5, 'ORDER_FILLING_IOC', None), getattr(mt5, 'ORDER_FILLING_RETURN', None)}:
                supported.append(int(filling_mode))
        for preferred in (getattr(mt5, 'ORDER_FILLING_RETURN', None), getattr(mt5, 'ORDER_FILLING_IOC', None), getattr(mt5, 'ORDER_FILLING_FOK', None)):
            if preferred is not None and preferred in supported:
                logging.info('MT5 FILLING MODE | symbol=%s selected=%s supported=%s', symbol, preferred, supported)
                return int(preferred)
        fallback = getattr(mt5, 'ORDER_FILLING_IOC', 1)
        logging.warning('MT5 FILLING MODE | symbol=%s selected=%s supported=%s reason=fallback', symbol, fallback, supported)
        return int(fallback)

    def _supported_filling_modes(self, symbol_info: Any) -> list[int]:
        supported_modes: list[int] = []

        def _push_supported(mode: Any) -> None:
            if isinstance(mode, int) and mode not in supported_modes:
                supported_modes.append(mode)

        filling_mode_mask = getattr(symbol_info, 'filling_mode', None)
        for candidate_name in ('ORDER_FILLING_IOC', 'ORDER_FILLING_FOK', 'ORDER_FILLING_RETURN'):
            candidate = getattr(mt5, candidate_name, None)
            if candidate is None:
                continue
            if filling_mode_mask == candidate or (
                isinstance(filling_mode_mask, int)
                and isinstance(candidate, int)
                and candidate > 0
                and filling_mode_mask > 0
                and filling_mode_mask & candidate == candidate
            ):
                _push_supported(int(candidate))
        return supported_modes

    def _validate_symbol_tradability(self, *, symbol: str, side: str, symbol_info: Any) -> str | None:
        trade_mode = getattr(symbol_info, 'trade_mode', None)
        if trade_mode is not None:
            if not isinstance(trade_mode, int):
                return 'invalid_trade_mode'
            known_trade_modes = {
                mode
                for mode in (
                    getattr(mt5, 'SYMBOL_TRADE_MODE_DISABLED', None),
                    getattr(mt5, 'SYMBOL_TRADE_MODE_LONGONLY', None),
                    getattr(mt5, 'SYMBOL_TRADE_MODE_SHORTONLY', None),
                    getattr(mt5, 'SYMBOL_TRADE_MODE_CLOSEONLY', None),
                    getattr(mt5, 'SYMBOL_TRADE_MODE_FULL', None),
                )
                if isinstance(mode, int)
            }
            if known_trade_modes and trade_mode not in known_trade_modes:
                return 'invalid_trade_mode'
            if trade_mode == getattr(mt5, 'SYMBOL_TRADE_MODE_DISABLED', object()):
                return 'symbol_trade_disabled'
            if trade_mode == getattr(mt5, 'SYMBOL_TRADE_MODE_CLOSEONLY', object()):
                return 'symbol_close_only'
            if side == LONG_SIDE and trade_mode == getattr(mt5, 'SYMBOL_TRADE_MODE_SHORTONLY', object()):
                return 'symbol_short_only'
            if side == SHORT_SIDE and trade_mode == getattr(mt5, 'SYMBOL_TRADE_MODE_LONGONLY', object()):
                return 'symbol_long_only'

        filling_mode = getattr(symbol_info, 'filling_mode', None)
        if not isinstance(filling_mode, int):
            return 'invalid_filling_mode'
        if not self._supported_filling_modes(symbol_info):
            return 'invalid_filling_mode'
        return None

    def _collect_filling_modes(self, symbol: str, symbol_info: Any, requested_mode: int | None = None) -> list[int]:
        modes: list[int] = []

        def _push(mode: Any) -> None:
            if isinstance(mode, int) and mode not in modes:
                modes.append(mode)

        _push(self.filling_mode_cache.get(symbol))
        _push(requested_mode)
        for mode in self._supported_filling_modes(symbol_info):
            _push(mode)
        # Preferred fallback order: IOC -> RETURN -> FOK
        _push(getattr(mt5, 'ORDER_FILLING_IOC', None))
        _push(getattr(mt5, 'ORDER_FILLING_RETURN', None))
        _push(getattr(mt5, 'ORDER_FILLING_FOK', None))
        return modes

    def send_order_with_fallback(self, request: dict[str, Any], symbol: str, symbol_info: Any, requested_mode: int | None = None, refresh_request: Any | None = None) -> tuple[Any, int | None]:
        candidate_modes: list[int] = self._collect_filling_modes(
            symbol=symbol,
            symbol_info=symbol_info,
            requested_mode=requested_mode,
        )

        if not candidate_modes:
            candidate_modes = [int(mt5.ORDER_FILLING_IOC)]
        last_result = None
        used_mode = None
        for attempt_idx, mode in enumerate(candidate_modes, start=1):
            req = dict(request)
            req['type_filling'] = int(mode)
            if callable(refresh_request):
                try:
                    refresh_request(req, attempt_idx)
                except Exception as exc:
                    logging.warning('MT5 ORDER REFRESH FAILED | symbol=%s filling=%s attempt=%d error=%s', symbol, mode, attempt_idx, exc)
            logging.info('MT5 ORDER ATTEMPT | symbol=%s filling=%s price=%.10f volume=%.12f', symbol, str(mode), float(req.get('price', 0.0)), float(req.get('volume', 0.0)))
            result = mt5.order_send(req)
            last_result = result
            used_mode = mode
            if result is None:
                logging.error('MT5 ORDER ATTEMPT FAILED | symbol=%s filling=%s reason=result_none', symbol, str(mode))
                continue
            retcode = _extract_result_retcode(result)
            if retcode is None:
                logging.error('MT5 ORDER ATTEMPT FAILED | symbol=%s filling=%s reason=invalid_retcode raw=%s', symbol, str(mode), str(getattr(result, 'retcode', None)))
                return result, used_mode
            comment = str(getattr(result, 'comment', ''))
            logging.info('MT5 ORDER ATTEMPT RESULT | symbol=%s filling=%s retcode=%s comment=%s', symbol, str(mode), str(retcode), comment)
            if retcode == mt5.TRADE_RETCODE_DONE:
                return result, used_mode
            if retcode == 10027:
                logging.critical('AUTO TRADING DISABLED IN MT5')
                return result, used_mode
            if retcode != 10030:
                return result, used_mode
        return last_result, used_mode

    def sync_position_state(self, symbol: str) -> NormalizedPosition:
        position = self.fetch_open_position(symbol)
        logging.info('MT5 POSITION SYNC | symbol=%s side=%s qty=%.4f entry=%.6f broker_id=%s', symbol, position.side, position.size, position.entry_price, position.broker_id or 'none')
        return position

    def _ensure_connected(self) -> None:
        if not self.connected or mt5 is None:
            raise RuntimeError('MT5 adapter is not connected')
        account_info = mt5.account_info()
        if account_info is None:
            error = _format_mt5_error(mt5.last_error())
            self._mark_disconnected(error)
            logging.critical('MT5 HEARTBEAT FAILURE | reason=account_info_none error=%s', error)
            raise RuntimeError(f'MT5 heartbeat failed: {error}')

    def validate_trading_session(self) -> None:
        self._ensure_connected()

    def validate_order_channel(self) -> int:
        self._ensure_connected()
        try:
            positions_total = mt5.positions_total()
        except Exception as exc:
            self._mark_disconnected(str(exc))
            logging.critical('MT5 ORDER CHANNEL FAILURE | reason=positions_total_exception error=%s', exc)
            raise RuntimeError(f'MT5 positions_total failed: {exc}') from exc
        if positions_total is None:
            error = _format_mt5_error(mt5.last_error())
            self._mark_disconnected(error)
            logging.critical('MT5 ORDER CHANNEL FAILURE | reason=positions_total_none error=%s', error)
            raise RuntimeError(f'MT5 positions_total failed: {error}')
        try:
            validated_total = int(positions_total)
        except (TypeError, ValueError) as exc:
            error = f'invalid_positions_total:{positions_total}'
            self._mark_disconnected(error)
            logging.critical('MT5 ORDER CHANNEL FAILURE | reason=positions_total_invalid error=%s', positions_total)
            raise RuntimeError(f'MT5 positions_total invalid: {positions_total}') from exc
        if validated_total < 0:
            error = f'invalid_positions_total:{validated_total}'
            self._mark_disconnected(error)
            logging.critical('MT5 ORDER CHANNEL FAILURE | reason=positions_total_negative error=%s', validated_total)
            raise RuntimeError(f'MT5 positions_total invalid: {validated_total}')
        return validated_total

    @staticmethod
    def _normalize_position(position: Any) -> NormalizedPosition:
        position_type = int(getattr(position, 'type', -1))
        side = LONG_SIDE if position_type == getattr(mt5, 'POSITION_TYPE_BUY', 0) else SHORT_SIDE if position_type == getattr(mt5, 'POSITION_TYPE_SELL', 1) else FLAT_SIDE
        raw = position._asdict() if hasattr(position, '_asdict') else {}
        return NormalizedPosition(
            symbol=str(getattr(position, 'symbol', '')),
            side=side,
            size=abs(float(getattr(position, 'volume', 0.0) or 0.0)),
            entry_price=float(getattr(position, 'price_open', 0.0) or 0.0),
            stop_loss=float(getattr(position, 'sl', 0.0) or 0.0) or None,
            take_profit=float(getattr(position, 'tp', 0.0) or 0.0) or None,
            broker_id=str(getattr(position, 'ticket', '') or '') or None,
            raw=raw,
        )


def round_size_to_step(size: float, step: float) -> float:
    if size <= 0 or step <= 0:
        return 0.0
    try:
        rounded = (_decimal_from_number(size) / _decimal_from_number(step)).to_integral_value(rounding=ROUND_DOWN) * _decimal_from_number(step)
    except (InvalidOperation, ZeroDivisionError):
        return 0.0
    return float(rounded)


def compute_safe_fx_lot(raw_lot: float, min_lot: float, lot_step: float) -> float:
    if raw_lot <= 0:
        return min_lot
    safe_lot = max(raw_lot, min_lot)
    if lot_step <= 0:
        adjusted_lot = safe_lot
    else:
        steps = math.ceil(safe_lot / lot_step)
        adjusted_lot = steps * lot_step
    if adjusted_lot < min_lot:
        adjusted_lot = min_lot
    return float(adjusted_lot)


def clamp_size(size: float, min_size: float, max_size: float | None) -> float:
    if size <= 0:
        return 0.0
    if min_size > 0 and size < min_size:
        return 0.0
    if max_size is not None and max_size > 0:
        return min(size, max_size)
    return size


def calculate_order_size_from_notional(notional_usd: float, position_scale: float, entry_price: float, step: float, min_qty: float) -> float:
    if entry_price <= 0:
        logging.warning('ORDER BLOCKED | invalid entry_price=%.12f', entry_price)
        return 0.0
    raw_qty = (float(notional_usd) * float(position_scale)) / float(entry_price)
    rounded_qty = round_size_to_step(raw_qty, step)
    return clamp_size(rounded_qty, min_qty, None)


def resolve_fx_pip_value_per_lot(*, specs: SymbolSpecs, entry_price: float) -> float:
    if specs.pip_value_per_lot is not None and specs.pip_value_per_lot > 0:
        return float(specs.pip_value_per_lot)
    pip_size = float(specs.pip_size or _pip_size_from_digits(specs.digits))
    contract_size = float(specs.contract_size or 0.0)
    if pip_size <= 0 or contract_size <= 0 or entry_price <= 0:
        raise ValueError('invalid FX symbol specs for pip value calculation')
    pair = _fx_pair_components(specs.symbol)
    if pair is None:
        raise ValueError(f'unsupported FX symbol for pip value calculation: {specs.symbol}')
    base_ccy, quote_ccy = pair
    account_ccy = str(specs.account_currency or 'USD').upper()
    quote_pip_value = contract_size * pip_size
    if quote_ccy == account_ccy:
        return quote_pip_value
    if base_ccy == account_ccy:
        return quote_pip_value / entry_price
    raise ValueError(f'cannot safely convert pip value for {specs.symbol} into {account_ccy}')


def calculate_fx_lot_size(*, risk_per_trade_usd: float | None, risk_pct: float | None, account_equity_usd: float, entry_price: float, stop_loss_price: float, specs: SymbolSpecs) -> float:
    if entry_price <= 0 or stop_loss_price <= 0:
        raise ValueError('entry_price and stop_loss_price must be > 0')
    risk_budget = float(risk_per_trade_usd or 0.0)
    if risk_budget <= 0 and risk_pct is not None and risk_pct > 0:
        risk_budget = float(account_equity_usd) * float(risk_pct)
    if risk_budget <= 0:
        raise ValueError('risk budget must be > 0')
    pip_size = float(specs.pip_size or _pip_size_from_digits(specs.digits))
    if pip_size <= 0:
        raise ValueError('invalid FX symbol specs for lot sizing')
    stop_distance = abs(float(entry_price) - float(stop_loss_price))
    if stop_distance <= 0:
        raise ValueError('stop loss distance must be > 0')
    stop_pips = stop_distance / pip_size
    if stop_pips <= 0:
        raise ValueError('stop pips must be > 0')
    pip_value_per_lot = resolve_fx_pip_value_per_lot(specs=specs, entry_price=float(entry_price))
    if pip_value_per_lot <= 0:
        raise ValueError('invalid pip value per lot')
    raw_lots = risk_budget / (stop_pips * pip_value_per_lot)
    min_lot = float(specs.minimum_size)
    lot_step = float(specs.size_step)
    rounded_lots = round_size_to_step(raw_lots, lot_step) if lot_step > 0 else float(raw_lots)
    if not math.isfinite(rounded_lots) or rounded_lots <= 0:
        raise ValueError('zero lot after conservative rounding')
    if min_lot > 0 and rounded_lots < min_lot:
        raise ValueError('zero lot after conservative rounding')
    safe_lots = float(rounded_lots)
    if specs.max_lot is not None and specs.max_lot > 0:
        safe_lots = min(safe_lots, float(specs.max_lot))
    if min_lot > 0 and safe_lots < min_lot:
        raise ValueError('zero lot after conservative rounding')
    return safe_lots


def normalize_mt5_symbol_info(symbol: str, info: Any) -> SymbolSpecs:
    point = float(getattr(info, 'point', 0.0) or 0.0)
    digits = int(getattr(info, 'digits', 0) or 0)
    pip_size = _pip_size_from_digits(digits)
    return SymbolSpecs(
        symbol=symbol,
        category='fx',
        qty_step=float(getattr(info, 'volume_step', 0.0) or 0.0),
        min_qty=float(getattr(info, 'volume_min', 0.0) or 0.0),
        tick_size=point,
        digits=digits,
        point=point,
        pip_size=pip_size,
        contract_size=float(getattr(info, 'trade_contract_size', 0.0) or 0.0),
        lot_step=float(getattr(info, 'volume_step', 0.0) or 0.0),
        min_lot=float(getattr(info, 'volume_min', 0.0) or 0.0),
        max_lot=float(getattr(info, 'volume_max', 0.0) or 0.0),
        trade_mode=str(getattr(info, 'trade_mode', '')),
    )


def sync_normalized_position(*, position: NormalizedPosition, active_trade: Any, active_notional_usd: float, risk_state: Any, recovery_factory: Any, logger: Any) -> tuple[Any, float, bool, bool]:
    bot_open = active_trade is not None and getattr(risk_state, 'open_positions', 0) > 0
    if not position.is_open and bot_open:
        risk_state.open_positions = 0
        logger.warning('SYNC FIX | reason=broker_flat_internal_open')
        return None, active_notional_usd, False, False
    if position.is_open and not bot_open:
        recovered_trade = recovery_factory(signal_score=1.0, volatility=0.001, side=position.side, entry_price=position.entry_price, source='broker_recovery')
        recovered_trade.entry_time = getattr(recovered_trade, 'entry_time', None) or __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
        risk_state.open_positions = 1
        logger.warning('SYNC FIX | reason=recovered_broker_position side=%s qty=%.4f price=%.6f', position.side, position.size, position.entry_price)
        return recovered_trade, position.entry_price * position.size, False, True
    if position.is_open and bot_open:
        expected_qty = float(active_notional_usd) / max(float(getattr(active_trade, 'entry_price', 0.0)), 1e-9)
        qty_tolerance = max(position.size * 0.01, 1e-9)
        mismatch = (str(getattr(active_trade, 'side', '')).upper() != position.side) or abs(expected_qty - position.size) > qty_tolerance
        return active_trade, active_notional_usd, mismatch, False
    return active_trade, active_notional_usd, False, False
