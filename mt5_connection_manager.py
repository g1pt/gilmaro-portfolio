from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Sequence

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    mt5 = None

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None


LOGGER = logging.getLogger(__name__)


def _format_mt5_error(error: Any) -> str:
    if isinstance(error, tuple) and len(error) >= 2:
        return f"{error[0]}:{error[1]}"
    return str(error)


@dataclass(frozen=True)
class MT5Credentials:
    login: int
    password: str
    server: str


@dataclass
class MT5ConnectionConfig:
    terminal_path: str | None = None
    terminal_fallback_paths: Sequence[str] = field(default_factory=lambda: (
        r"C:\\Program Files\\MetaTrader 5\\terminal64.exe",
        r"C:\\Program Files (x86)\\MetaTrader 5\\terminal64.exe",
        r"C:\\Program Files\\IC Markets MetaTrader 5\\terminal64.exe",
        r"C:\\Program Files\\Pepperstone MetaTrader 5\\terminal64.exe",
    ))
    terminal_launch_timeout_sec: float = 40.0
    initialize_timeout_ms: int = 60_000
    connection_timeout_sec: float = 60.0
    login_timeout_sec: float = 45.0
    retry_attempts: int = 4
    base_retry_delay_sec: float = 1.0
    max_retry_delay_sec: float = 12.0
    watchdog_interval_sec: float = 5.0
    watchdog_enabled: bool = True
    headless: bool = False
    service_mode: bool = False
    symbol_alias_delimiters: Sequence[str] = field(default_factory=lambda: (".", "_", "-"))

    @classmethod
    def from_env(cls) -> "MT5ConnectionConfig":
        env_paths: list[str] = []
        raw_fallback = os.getenv("MT5_PATH_FALLBACKS", "").strip()
        if raw_fallback:
            env_paths = [x.strip() for x in raw_fallback.split(";") if x.strip()]

        return cls(
            terminal_path=os.getenv("MT5_PATH") or None,
            terminal_fallback_paths=tuple(env_paths) if env_paths else cls().terminal_fallback_paths,
            terminal_launch_timeout_sec=float(os.getenv("MT5_TERMINAL_LAUNCH_TIMEOUT_SEC", "40")),
            initialize_timeout_ms=int(os.getenv("MT5_TIMEOUT_MS", "60000")),
            connection_timeout_sec=float(os.getenv("MT5_CONNECTION_TIMEOUT_SEC", "60")),
            login_timeout_sec=float(os.getenv("MT5_LOGIN_TIMEOUT_SEC", "45")),
            retry_attempts=int(os.getenv("MT5_RETRY_ATTEMPTS", "4")),
            base_retry_delay_sec=float(os.getenv("MT5_RETRY_BASE_DELAY_SEC", "1")),
            max_retry_delay_sec=float(os.getenv("MT5_RETRY_MAX_DELAY_SEC", "12")),
            watchdog_interval_sec=float(os.getenv("MT5_WATCHDOG_INTERVAL_SEC", "5")),
            watchdog_enabled=os.getenv("MT5_WATCHDOG_ENABLED", "1").strip().lower() not in {"0", "false", "no"},
            headless=os.getenv("MT5_HEADLESS", "0").strip().lower() in {"1", "true", "yes"},
            service_mode=os.getenv("MT5_SERVICE_MODE", "0").strip().lower() in {"1", "true", "yes"},
        )


class MT5ConnectionManager:
    """Automated MetaTrader5 terminal + session lifecycle manager."""

    def __init__(
        self,
        *,
        credentials: MT5Credentials | None = None,
        requested_symbols: Sequence[str] | None = None,
        config: MT5ConnectionConfig | None = None,
    ) -> None:
        self.config = config or MT5ConnectionConfig.from_env()
        self.credentials = credentials or self._credentials_from_env()
        self.requested_symbols = list(requested_symbols or [])

        self._lock = threading.RLock()
        self._terminal_path = self._resolve_terminal_path(self.config.terminal_path)
        self._terminal_process: subprocess.Popen[str] | None = None
        self._connected = False
        self._symbol_map: dict[str, str] = {}

        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def symbol_map(self) -> dict[str, str]:
        with self._lock:
            return dict(self._symbol_map)

    def connect(self) -> bool:
        with self._lock:
            if mt5 is None:
                raise RuntimeError("MetaTrader5 package unavailable")

            return self._connect_with_recovery()

    def ensure_connection(self) -> bool:
        with self._lock:
            connection_alive = self._is_connection_alive()
            symbols_ready = self._are_symbols_ready()
            if connection_alive and symbols_ready:
                self._connected = True
                return True
            self._connected = False
            return self._connect_with_recovery()

    def shutdown(self) -> None:
        with self._lock:
            self.stop_watchdog()
            if mt5 is not None:
                try:
                    mt5.shutdown()
                except Exception:
                    pass
            self._connected = False

    def start_watchdog(self) -> None:
        if not self.config.watchdog_enabled:
            return
        with self._lock:
            if self._watchdog_thread and self._watchdog_thread.is_alive():
                return
            self._watchdog_stop.clear()
            self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="mt5-watchdog", daemon=True)
            self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        if thread and thread.is_alive():
            thread.join(timeout=max(self.config.watchdog_interval_sec * 2, 1.0))

    def run_service_mode(self, *, stop_event: threading.Event | None = None) -> None:
        """Blocking supervisor loop, suitable for Windows Service/NSSM usage."""
        final_stop_event = stop_event or threading.Event()
        self.start_watchdog()
        while not final_stop_event.wait(timeout=self.config.watchdog_interval_sec):
            self.ensure_connection()

    def bootstrap_symbols(self, requested_symbols: Sequence[str] | None = None) -> dict[str, str]:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package unavailable")

        symbols = list(requested_symbols) if requested_symbols is not None else self.requested_symbols
        available = mt5.symbols_get()
        if available is None:
            self._log("MT5_FAILED", reason="symbols_get_failed")
            raise RuntimeError(f"mt5.symbols_get failed: {self._last_error_text()}")

        available_names = [str(getattr(sym, "name", "") or "") for sym in available if getattr(sym, "name", None)]
        available_set = {name.upper(): name for name in available_names}

        resolved_map: dict[str, str] = {}
        for requested in symbols:
            resolved = self._resolve_symbol(requested, available_set)
            if resolved is None:
                self._log("MT5_FAILED", reason="symbol_not_found", requested_symbol=requested)
                continue
            if not mt5.symbol_select(resolved, True):
                self._log("MT5_FAILED", reason="symbol_select_failed", requested_symbol=requested, resolved_symbol=resolved)
                continue
            resolved_map[requested] = resolved

        with self._lock:
            self._symbol_map = resolved_map
        return resolved_map

    def get_symbol(self, requested_symbol: str) -> str:
        with self._lock:
            existing = self._symbol_map.get(requested_symbol)
        if existing:
            return existing
        mapping = self.bootstrap_symbols([requested_symbol])
        if requested_symbol not in mapping:
            raise KeyError(f"No MT5 symbol match found for {requested_symbol}")
        return mapping[requested_symbol]

    def _connect_with_recovery(self) -> bool:
        attempts = max(self.config.retry_attempts, 1)
        for attempt in range(1, attempts + 1):
            try:
                self._ensure_terminal_started()
                self._initialize_or_recover()
                self._ensure_login()
                self.bootstrap_symbols()
                if not self._are_symbols_ready():
                    raise RuntimeError("symbols_not_ready_after_bootstrap")
                self._connected = True
                self._log("MT5_CONNECTED", attempt=attempt, account=self.credentials.login)
                return True
            except Exception as exc:
                self._connected = False
                self._log("MT5_FAILED", attempt=attempt, error=str(exc), mt5_error=self._last_error_text())
                if attempt >= attempts or self._is_unrecoverable_connection_error(exc):
                    return False
                try:
                    self._restart_terminal_process()
                except Exception as restart_exc:
                    self._log(
                        "MT5_FAILED",
                        attempt=attempt,
                        reason="restart_failed",
                        error=str(restart_exc),
                        previous_error=str(exc),
                        mt5_error=self._last_error_text(),
                    )
                    return False
                delay = min(self.config.base_retry_delay_sec * (2 ** (attempt - 1)), self.config.max_retry_delay_sec)
                time.sleep(delay)
        return False

    def _initialize_or_recover(self) -> None:
        assert mt5 is not None

        initialized = mt5.initialize(path=self._terminal_path, timeout=self.config.initialize_timeout_ms)
        if initialized:
            return

        self._log("MT5_FAILED", reason="initialize_failed", mt5_error=self._last_error_text())
        try:
            self._restart_terminal_process()
        except Exception as exc:
            raise RuntimeError(f"initialize_recovery_failed: {exc}") from exc
        initialized = mt5.initialize(path=self._terminal_path, timeout=self.config.initialize_timeout_ms)
        if not initialized:
            raise RuntimeError(f"initialize_failed_after_restart: {self._last_error_text()}")

    def _ensure_login(self) -> None:
        assert mt5 is not None
        current = mt5.account_info()
        if current is not None and int(getattr(current, "login", 0) or 0) == self.credentials.login:
            return

        deadline = time.monotonic() + self.config.login_timeout_sec
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            ok = mt5.login(
                login=int(self.credentials.login),
                password=self.credentials.password,
                server=self.credentials.server,
            )
            if ok:
                verified = mt5.account_info()
                if verified is not None and int(getattr(verified, "login", 0) or 0) == self.credentials.login:
                    return

            last_error = mt5.last_error()
            code = int(last_error[0]) if isinstance(last_error, tuple) and last_error else -1
            message = _format_mt5_error(last_error)

            if self._is_invalid_credentials_error(code, message):
                raise RuntimeError(f"invalid_credentials: {message}")

            wait_sec = min(self.config.base_retry_delay_sec * (2 ** max(attempt - 1, 0)), self.config.max_retry_delay_sec)
            time.sleep(wait_sec)

        raise TimeoutError(f"login_timeout_after_{attempt}_attempts: {self._last_error_text()}")

    def _ensure_terminal_started(self) -> None:
        if self._is_terminal_process_alive():
            return

        launch_args = [self._terminal_path]
        if self.config.headless:
            launch_args.extend(["/portable", "/skipupdate"])

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW if self.config.headless else 0

        self._terminal_process = subprocess.Popen(
            launch_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            text=True,
        )
        self._log("MT5_START", terminal_path=self._terminal_path, pid=self._terminal_process.pid)

        deadline = time.monotonic() + self.config.terminal_launch_timeout_sec
        while time.monotonic() < deadline:
            if self._is_terminal_process_alive():
                return
            time.sleep(0.25)

        raise TimeoutError("terminal_start_timeout")

    def _restart_terminal_process(self) -> None:
        self._kill_terminal_processes()
        if mt5 is not None:
            try:
                mt5.shutdown()
            except Exception:
                pass
        time.sleep(0.75)
        self._ensure_terminal_started()
        self._log("MT5_RECOVERED", action="terminal_restart")

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self.config.watchdog_interval_sec):
            try:
                with self._lock:
                    terminal_alive = self._is_terminal_process_alive()
                    connection_alive = self._is_connection_alive()
                    symbols_ready = self._are_symbols_ready()
                    if not terminal_alive or not connection_alive or not symbols_ready:
                        self._connected = False
                        self._log(
                            "MT5_FAILED",
                            reason="watchdog_recovery_triggered",
                            terminal_alive=terminal_alive,
                            connection_alive=connection_alive,
                            symbols_ready=symbols_ready,
                        )
                        if not self._connect_with_recovery():
                            self._connected = False
            except Exception as exc:
                self._connected = False
                self._log("MT5_FAILED", reason="watchdog_exception", error=str(exc), mt5_error=self._last_error_text())

    def _is_connection_alive(self) -> bool:
        if mt5 is None:
            return False
        try:
            account = mt5.account_info()
            if account is None:
                return False
            terminal = mt5.terminal_info()
            if terminal is not None and hasattr(terminal, "connected") and not bool(getattr(terminal, "connected", False)):
                return False
            return True
        except Exception:
            return False

    def _are_symbols_ready(self) -> bool:
        if mt5 is None:
            return False
        if self.requested_symbols and any(requested not in self._symbol_map for requested in self.requested_symbols):
            return False
        for resolved in self._symbol_map.values():
            info = mt5.symbol_info(resolved)
            if info is None:
                return False
            if not bool(getattr(info, "visible", False)) and not mt5.symbol_select(resolved, True):
                return False
        return True

    def _resolve_symbol(self, requested: str, available_set: dict[str, str]) -> str | None:
        normalized = requested.upper()
        if normalized in available_set:
            return available_set[normalized]

        candidates: list[str] = []
        for key, original in available_set.items():
            if key.startswith(normalized):
                candidates.append(original)
                continue
            for delimiter in self.config.symbol_alias_delimiters:
                if key.startswith(f"{normalized}{delimiter}"):
                    candidates.append(original)
                    break

        if not candidates:
            return None

        candidates.sort(key=lambda x: (len(x), x))
        return candidates[0]

    def _is_terminal_process_alive(self) -> bool:
        if psutil is not None:
            for process in psutil.process_iter(attrs=["name"]):
                name = str(process.info.get("name") or "").lower()
                if name == "terminal64.exe":
                    return True
            return False

        if os.name == "nt":
            try:
                result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe"], capture_output=True, text=True, check=False)
            except Exception:
                return False
            return "terminal64.exe" in result.stdout.lower()

        if self._terminal_process is None:
            return False
        return self._terminal_process.poll() is None

    def _kill_terminal_processes(self) -> None:
        if psutil is not None:
            for process in psutil.process_iter(attrs=["name"]):
                try:
                    name = str(process.info.get("name") or "").lower()
                    if name == "terminal64.exe":
                        process.kill()
                except Exception:
                    continue
            return

        if os.name == "nt":
            subprocess.run(["taskkill", "/IM", "terminal64.exe", "/F"], capture_output=True, text=True, check=False)
            return

        if self._terminal_process is not None and self._terminal_process.poll() is None:
            self._terminal_process.kill()

    def _resolve_terminal_path(self, configured: str | None) -> str:
        candidates: list[str] = []
        if configured:
            candidates.append(configured)
        candidates.extend(self.config.terminal_fallback_paths)
        candidates.extend(self._autodetect_terminal_paths())

        for candidate in self._dedupe(candidates):
            if Path(candidate).exists():
                return str(Path(candidate))

        if configured and self._uses_injected_mt5_backend():
            return str(configured)

        raise FileNotFoundError("terminal64.exe not found; set MT5_PATH")

    @staticmethod
    def _uses_injected_mt5_backend() -> bool:
        return mt5 is not None and not isinstance(mt5, ModuleType)

    def _autodetect_terminal_paths(self) -> list[str]:
        candidates: list[str] = []
        roaming = os.getenv("APPDATA", "")
        if roaming:
            base = Path(roaming) / "MetaQuotes" / "Terminal"
            if base.exists():
                for item in base.iterdir():
                    if not item.is_dir():
                        continue
                    probe = item / "terminal64.exe"
                    if probe.exists():
                        candidates.append(str(probe))
        return candidates

    @staticmethod
    def _dedupe(values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for value in values:
            key = str(value).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            output.append(value)
        return output

    @staticmethod
    def _is_invalid_credentials_error(code: int, message: str) -> bool:
        credential_codes = {-6, 64, 4013, 10004}
        lowered = message.lower()
        return code in credential_codes or "invalid account" in lowered or "invalid password" in lowered or "authorization failed" in lowered

    @staticmethod
    def _is_unrecoverable_connection_error(exc: Exception) -> bool:
        if isinstance(exc, FileNotFoundError):
            return True
        message = str(exc).lower()
        return message.startswith("invalid_credentials:") or message.startswith("initialize_recovery_failed:")

    def _last_error_text(self) -> str:
        if mt5 is None:
            return "package_unavailable"
        try:
            return _format_mt5_error(mt5.last_error())
        except Exception:
            return "unavailable"

    @staticmethod
    def _credentials_from_env() -> MT5Credentials:
        login_raw = os.getenv("MT5_LOGIN", "0").strip()
        password = os.getenv("MT5_PASSWORD", "")
        server = os.getenv("MT5_SERVER", "")
        if not login_raw or int(login_raw) <= 0 or not password or not server:
            raise ValueError("MT5 credentials missing. Expected MT5_LOGIN, MT5_PASSWORD, MT5_SERVER")
        return MT5Credentials(login=int(login_raw), password=password, server=server)

    def _log(self, event: str, **fields: Any) -> None:
        payload = {
            "event": event,
            "mt5_error": self._last_error_text(),
            **fields,
        }
        LOGGER.info("%s | %s", event, payload)
