from __future__ import annotations

from types import SimpleNamespace

import mt5_connection_manager as manager_mod
from mt5_connection_manager import MT5ConnectionConfig, MT5ConnectionManager, MT5Credentials


class FakeMT5:
    def __init__(self) -> None:
        self._account = None
        self._symbols = [SimpleNamespace(name="EURUSD.a"), SimpleNamespace(name="XAUUSD")]
        self._selected: set[str] = set()

    def initialize(self, path: str, timeout: int) -> bool:
        return True

    def shutdown(self) -> bool:
        return True

    def login(self, login: int, password: str, server: str) -> bool:
        if password == "bad":
            return False
        self._account = SimpleNamespace(login=login, server=server)
        return True

    def account_info(self):
        return self._account

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def symbols_get(self):
        return self._symbols

    def symbol_select(self, symbol: str, visible: bool) -> bool:
        if symbol not in {"EURUSD.a", "XAUUSD"}:
            return False
        self._selected.add(symbol)
        return True

    def symbol_info(self, symbol: str):
        if symbol in self._selected:
            return SimpleNamespace(visible=True)
        return None

    def last_error(self):
        return (0, "ok")


class InvalidCredentialMT5(FakeMT5):
    def last_error(self):
        return (-6, "authorization failed")


class InitializeFailureMT5(FakeMT5):
    def initialize(self, path: str, timeout: int) -> bool:
        return False

    def last_error(self):
        return (-10005, "IPC initialize failed")


class OneShotStop:
    def __init__(self) -> None:
        self.calls = 0

    def wait(self, timeout: float) -> bool:
        self.calls += 1
        return self.calls > 1


def _build_manager() -> MT5ConnectionManager:
    cfg = MT5ConnectionConfig(
        terminal_path="/bin/sh",
        terminal_fallback_paths=(),
        watchdog_enabled=False,
        retry_attempts=1,
        login_timeout_sec=0.1,
    )
    mgr = MT5ConnectionManager(
        credentials=MT5Credentials(login=12345, password="good", server="Demo-Server"),
        requested_symbols=["EURUSD", "XAUUSD"],
        config=cfg,
    )
    mgr._terminal_path = "/bin/sh"
    mgr._ensure_terminal_started = lambda: None
    mgr._restart_terminal_process = lambda: None
    return mgr


def test_bootstrap_symbols_maps_alias_and_selects(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(manager_mod, "mt5", fake)

    mgr = _build_manager()
    mapped = mgr.bootstrap_symbols(["EURUSD", "XAUUSD"])

    assert mapped["EURUSD"] == "EURUSD.a"
    assert mapped["XAUUSD"] == "XAUUSD"


def test_connect_success(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(manager_mod, "mt5", fake)

    mgr = _build_manager()
    assert mgr.connect() is True
    assert mgr.connected is True


def test_connect_fails_on_invalid_credentials(monkeypatch):
    fake = InvalidCredentialMT5()
    monkeypatch.setattr(manager_mod, "mt5", fake)

    cfg = MT5ConnectionConfig(
        terminal_path="/bin/sh",
        terminal_fallback_paths=(),
        watchdog_enabled=False,
        retry_attempts=1,
        login_timeout_sec=0.1,
    )
    mgr = MT5ConnectionManager(
        credentials=MT5Credentials(login=12345, password="bad", server="Demo-Server"),
        requested_symbols=["EURUSD"],
        config=cfg,
    )
    mgr._terminal_path = "/bin/sh"
    mgr._ensure_terminal_started = lambda: None
    mgr._restart_terminal_process = lambda: None

    assert mgr.connect() is False
    assert mgr.connected is False


def test_connect_invalid_credentials_does_not_restart_terminal(monkeypatch):
    fake = InvalidCredentialMT5()
    monkeypatch.setattr(manager_mod, "mt5", fake)

    cfg = MT5ConnectionConfig(
        terminal_path="/bin/sh",
        terminal_fallback_paths=(),
        watchdog_enabled=False,
        retry_attempts=3,
        login_timeout_sec=0.1,
    )
    mgr = MT5ConnectionManager(
        credentials=MT5Credentials(login=12345, password="bad", server="Demo-Server"),
        requested_symbols=["EURUSD"],
        config=cfg,
    )
    mgr._terminal_path = "/bin/sh"
    mgr._ensure_terminal_started = lambda: None
    restart_calls: list[str] = []
    mgr._restart_terminal_process = lambda: restart_calls.append("restart")

    assert mgr.connect() is False
    assert mgr.connected is False
    assert restart_calls == []


def test_connect_returns_false_when_initialize_recovery_fails(monkeypatch):
    fake = InitializeFailureMT5()
    monkeypatch.setattr(manager_mod, "mt5", fake)

    cfg = MT5ConnectionConfig(
        terminal_path="/bin/sh",
        terminal_fallback_paths=(),
        watchdog_enabled=False,
        retry_attempts=2,
        login_timeout_sec=0.1,
    )
    mgr = MT5ConnectionManager(
        credentials=MT5Credentials(login=12345, password="good", server="Demo-Server"),
        requested_symbols=["EURUSD"],
        config=cfg,
    )
    mgr._terminal_path = "/bin/sh"
    mgr._ensure_terminal_started = lambda: None

    def fail_restart() -> None:
        raise RuntimeError("terminal_restart_failed")

    mgr._restart_terminal_process = fail_restart

    assert mgr.connect() is False
    assert mgr.connected is False


def test_ensure_connection_restores_ready_flag_when_session_and_symbols_are_healthy(monkeypatch):
    fake = FakeMT5()
    fake._account = SimpleNamespace(login=12345, server="Demo-Server")
    fake._selected.update({"EURUSD.a", "XAUUSD"})
    monkeypatch.setattr(manager_mod, "mt5", fake)

    mgr = _build_manager()
    mgr._connected = False
    mgr._symbol_map = {"EURUSD": "EURUSD.a", "XAUUSD": "XAUUSD"}

    assert mgr.ensure_connection() is True
    assert mgr.connected is True


def test_connect_fails_when_requested_symbol_bootstrap_is_incomplete(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(manager_mod, "mt5", fake)

    cfg = MT5ConnectionConfig(
        terminal_path="/bin/sh",
        terminal_fallback_paths=(),
        watchdog_enabled=False,
        retry_attempts=1,
        login_timeout_sec=0.1,
    )
    mgr = MT5ConnectionManager(
        credentials=MT5Credentials(login=12345, password="good", server="Demo-Server"),
        requested_symbols=["EURUSD", "GBPUSD"],
        config=cfg,
    )
    mgr._terminal_path = "/bin/sh"
    mgr._ensure_terminal_started = lambda: None
    mgr._restart_terminal_process = lambda: None

    assert mgr.connect() is False
    assert mgr.connected is False


def test_watchdog_clears_ready_flag_when_recovery_fails(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(manager_mod, "mt5", fake)

    mgr = _build_manager()
    mgr._connected = True
    mgr._watchdog_stop = OneShotStop()
    mgr._is_terminal_process_alive = lambda: True
    mgr._is_connection_alive = lambda: False
    mgr._are_symbols_ready = lambda: False
    mgr._connect_with_recovery = lambda: False

    mgr._watchdog_loop()

    assert mgr.connected is False
