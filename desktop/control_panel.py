"""Minimal Server Control 2 shell: status, direct consoles and users."""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from api import ApiClient, ApiError
from pages_base import BasePage
from pages_console_v2 import ConsolePage
from pages_dashboard_v2 import DashboardPage
from pages_users_v2 import AccountPage, UsersPage
from state import AppState, LocalPreferences


class ControlPanel(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        api: ApiClient,
        user: dict[str, Any],
        preferences: LocalPreferences,
        logout: Callable[[], None],
        check_client_update: Callable[[], None],
        client_version: str,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self.state = AppState(dict(user))
        self.preferences = preferences
        self.logout_callback = logout
        self.check_client_update = check_client_update
        self.client_version = client_version
        self.closed = False
        self.current_page = "dashboard"
        self.pages: dict[str, BasePage] = {}
        self.page_buttons: dict[str, ttk.Button] = {}
        self._neco_image: tk.PhotoImage | None = None
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._status_inflight = False
        self._power_inflight = False
        self._session_inflight = False
        self._status_after: str | None = None
        self._power_after: str | None = None
        self._session_after: str | None = None
        self._ui_after: str | None = None
        self._message_after: str | None = None

        self.connection_var = tk.StringVar(value="Подключение…")
        self.message_var = tk.StringVar(value="Загружаю состояние сервера…")
        self.identity_var = tk.StringVar()
        self.page_title_var = tk.StringVar(value="Состояние")
        self._build()
        self._ui_after = self.after(30, self._drain_ui_queue)
        self.after(40, self.refresh_now)
        self.after(500, self._poll_power)
        self.after(1500, self._validate_session)

    def _build(self) -> None:
        sidebar = ttk.Frame(self, style="Sidebar.TFrame", padding=(18, 24), width=276)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = ttk.Frame(sidebar, style="Sidebar.TFrame")
        brand.pack(fill="x")
        ttk.Label(brand, text="◆", style="SidebarAccent.TLabel").pack(side="left")
        ttk.Label(brand, text="Server Control", style="SidebarTitle.TLabel").pack(side="left", padx=(10, 0), pady=(4, 0))
        self._update_identity_label()
        identity = ttk.Frame(sidebar, style="Sidebar.TFrame")
        identity.pack(fill="x", pady=(10, 24))
        ttk.Label(identity, text="●", style="SidebarOnline.TLabel").pack(side="left")
        ttk.Label(identity, textvariable=self.identity_var, style="SidebarSubtle.TLabel").pack(side="left", padx=(7, 0))

        nav = ttk.Frame(sidebar, style="Sidebar.TFrame")
        nav.pack(fill="x")

        try:
            resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            source = resource_root / "assets" / "neco_arc_sitting.png"
            original = tk.PhotoImage(file=str(source))
            self._neco_image = original.subsample(6, 6)
            ttk.Label(sidebar, image=self._neco_image, style="SidebarSubtle.TLabel").pack(side="bottom", pady=(12, 0))
        except (OSError, tk.TclError):
            self._neco_image = None

        self.content = ttk.Frame(self)
        self.content.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(self.content, style="Header.TFrame", padding=(34, 26, 34, 12))
        header.pack(fill="x")
        ttk.Label(header, textvariable=self.page_title_var, font=("Segoe UI Semibold", 25)).pack(side="left")
        self.connection_label = ttk.Label(header, textvariable=self.connection_var, style="Connection.TLabel")
        self.connection_label.pack(side="left", padx=18)
        ttk.Button(header, text="↻  Проверить обновление", style="Accent.TButton", command=self.check_client_update).pack(side="right")

        self.page_container = ttk.Frame(self.content, padding=(18, 2, 18, 10))
        self.page_container.pack(fill="both", expand=True)
        footer = ttk.Frame(self.content, style="Footer.TFrame", padding=(26, 12))
        footer.pack(fill="x")
        ttk.Label(footer, text="◌", style="FooterSubtle.TLabel", font=("Segoe UI", 15)).pack(side="left", padx=(0, 10))
        ttk.Label(footer, textvariable=self.message_var, style="FooterSubtle.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Label(footer, text=f"v{self.client_version}", style="FooterSubtle.TLabel").pack(side="right", padx=12)
        ttk.Button(footer, text="Выйти", command=self.logout_callback).pack(side="right")

        page_types: list[type[BasePage]] = [DashboardPage]
        if self.state.has_permission("terminal.linux") or self.state.has_permission("terminal.minecraft"):
            page_types.append(ConsolePage)
        if self.state.has_permission("users.manage"):
            page_types.append(UsersPage)
        page_types.append(AccountPage)
        nav_icons = {"dashboard": "⌂", "console": "▣", "users": "♙", "account": "○"}
        for page_type in page_types:
            page = page_type(self.page_container, self)
            self.pages[page.page_id] = page
            button = ttk.Button(
                nav,
                text=f"{nav_icons.get(page.page_id, '•')}   {page.title}",
                style="Nav.TButton",
                command=lambda name=page.page_id: self.select_page(name),
            )
            button.pack(fill="x", pady=2)
            self.page_buttons[page.page_id] = button
        self.select_page("dashboard")

    def select_page(self, name: str) -> None:
        page = self.pages.get(name)
        if not page:
            return
        for value in self.pages.values():
            value.pack_forget()
        page.pack(fill="both", expand=True)
        self.current_page = name
        self.page_title_var.set(page.title)
        for page_id, button in self.page_buttons.items():
            button.configure(style="NavActive.TButton" if page_id == name else "Nav.TButton")
        page.on_show()

    def _update_identity_label(self) -> None:
        role = "владелец" if self.state.user.get("role") == "owner" else "пользователь"
        self.identity_var.set(f"{self.state.user.get('username', '—')} · {role}")

    def update_identity(self, user: dict[str, Any]) -> None:
        old_permissions = set(self.state.user.get("permissions", []))
        self.state.user = dict(user)
        self._update_identity_label()
        new_permissions = set(user.get("permissions", []))
        if old_permissions != new_permissions:
            console = self.pages.get("console")
            if console and hasattr(console, "close"):
                console.close()
            self.status("Права изменились. Активные консоли закрыты; повторно войдите для обновления меню.")
        account = self.pages.get("account")
        if account:
            account.on_show()

    def run_async(
        self,
        work: Callable[[], Any],
        success: Callable[[Any], None],
        failure: Callable[[Exception], None] | None = None,
    ) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as error:
                if not self.closed:
                    self._ui_queue.put(
                        lambda caught=error: failure(caught) if failure else self.status(str(caught), error=True)
                    )
            else:
                if not self.closed:
                    self._ui_queue.put(lambda completed=result: success(completed))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_ui_queue(self) -> None:
        if self.closed:
            return
        for _index in range(300):
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except tk.TclError:
                pass
        self._ui_after = self.after(30, self._drain_ui_queue)

    def _poll_delay(self) -> int:
        try:
            minimized = self.winfo_toplevel().state() == "iconic"
        except tk.TclError:
            minimized = False
        return 5000 if minimized else 1000

    def refresh_now(self) -> None:
        if self.closed or self._status_inflight:
            return
        if not self.state.has_permission("status.view"):
            self.connection_var.set("Вход выполнен")
            self.message_var.set("Для этой учётной записи просмотр состояния отключён.")
            return
        self._status_inflight = True
        started = time.monotonic()

        def success(payload: dict[str, Any]) -> None:
            self._status_inflight = False
            self.state.latency_ms = max(0, round((time.monotonic() - started) * 1000))
            self.state.apply_server_snapshot(payload)
            self.connection_var.set("Подключено")
            self.connection_label.configure(style="Connection.TLabel")
            self._update_pages()
            self._schedule_status()

        def failure(error: Exception) -> None:
            self._status_inflight = False
            self.state.mark_disconnected(error)
            self.connection_var.set("Нет связи")
            self.connection_label.configure(style="ConnectionError.TLabel")
            self._update_pages()
            self._schedule_status()

        self.run_async(
            lambda: self.api.request("GET", "/v1/server/status", timeout_seconds=8),
            success,
            failure,
        )

    def _schedule_status(self) -> None:
        if self.closed:
            return
        if self._status_after:
            try:
                self.after_cancel(self._status_after)
            except tk.TclError:
                pass
        self._status_after = self.after(self._poll_delay(), self.refresh_now)

    def _poll_power(self) -> None:
        if self.closed:
            return
        if self._power_inflight or not self.state.has_permission("status.view"):
            self._power_after = self.after(5000, self._poll_power)
            return
        self._power_inflight = True

        def success(payload: dict[str, Any]) -> None:
            self._power_inflight = False
            self.state.apply_power(payload)
            self._update_pages()
            self._power_after = self.after(5000, self._poll_power)

        def failure(_error: Exception) -> None:
            self._power_inflight = False
            self._power_after = self.after(5000, self._poll_power)

        self.run_async(
            lambda: self.api.request("GET", "/v1/power/status", timeout_seconds=9),
            success,
            failure,
        )

    def _validate_session(self) -> None:
        if self.closed:
            return
        if self._session_inflight:
            self._session_after = self.after(5000, self._validate_session)
            return
        self._session_inflight = True

        def success(payload: dict[str, Any]) -> None:
            self._session_inflight = False
            user = payload.get("user")
            if isinstance(user, dict) and user != self.state.user:
                self.update_identity(user)
            self._session_after = self.after(5000, self._validate_session)

        def failure(error: Exception) -> None:
            self._session_inflight = False
            if isinstance(error, ApiError) and error.code in {"access_revoked", "invalid_session", "authentication_required"}:
                self.close()
                messagebox.showwarning("Доступ отключён", str(error))
                self.logout_callback()
                return
            self._session_after = self.after(5000, self._validate_session)

        self.run_async(lambda: self.api.request("GET", "/v1/me", timeout_seconds=8), success, failure)

    def _update_pages(self) -> None:
        for page in self.pages.values():
            try:
                page.update_state()
            except tk.TclError:
                pass

    def power_action(self, on: bool) -> None:
        title = "Включить питание сервера?" if on else "Безопасно выключить сервер и питание?"
        if not messagebox.askyesno("Питание сервера", title, icon="warning" if not on else "question"):
            return
        self.status("Отправляю команду питания…")

        def success(payload: dict[str, Any]) -> None:
            power = payload.get("power")
            if isinstance(power, dict):
                self.state.apply_power({"power": power})
            self.status("Питание включено" if on else "Начато безопасное выключение")
            self._update_pages()

        self.run_async(
            lambda: self.api.request("POST", "/v1/power/action", {"state": "on" if on else "off"}),
            success,
            lambda error: self.status(str(error), error=True),
        )

    def status(self, message: str, *, error: bool = False, seconds: int = 10) -> None:
        self.message_var.set(("Ошибка: " if error else "") + message)
        if self._message_after:
            try:
                self.after_cancel(self._message_after)
            except tk.TclError:
                pass
        self._message_after = self.after(seconds * 1000, lambda: self.message_var.set("Готово"))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for page in self.pages.values():
            close = getattr(page, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        for identifier in (
            self._status_after,
            self._power_after,
            self._session_after,
            self._ui_after,
            self._message_after,
        ):
            if identifier:
                try:
                    self.after_cancel(identifier)
                except tk.TclError:
                    pass
