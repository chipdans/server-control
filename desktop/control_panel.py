"""Sidebar shell, consolidated realtime sync and command palette."""

from __future__ import annotations

import threading
import time
import queue
import tkinter as tk
import urllib.parse
from tkinter import messagebox, ttk
from typing import Any, Callable

from api import ApiClient, ApiError
from pages_admin import JobsPage, LogsPage, SettingsPage, UpdatesPage, UsersPage
from pages_dashboard import DashboardPage
from pages_files import BackupsPage, FilesPage
from pages_minecraft import ConsolePage, InstancesPage, PlayersPage
from pages_system import MonitoringPage, ServerPage
from state import AppState, LocalPreferences
from widgets import CommandPalette


class ControlPanel(ttk.Frame):
    PAGE_TYPES = (
        DashboardPage,
        ServerPage,
        InstancesPage,
        ConsolePage,
        FilesPage,
        BackupsPage,
        MonitoringPage,
        PlayersPage,
        UsersPage,
        LogsPage,
        JobsPage,
        UpdatesPage,
        SettingsPage,
    )

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
        self.sync_inflight = False
        self.sync_after: str | None = None
        self.power_inflight = False
        self.power_after: str | None = None
        self.events_inflight = False
        self.events_after: str | None = None
        self.notifications_inflight = False
        self.notifications_after: str | None = None
        self._status_clear_after: str | None = None
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._ui_after: str | None = None
        self._page_buttons: dict[str, ttk.Button] = {}
        self.pages: dict[str, BasePage] = {}
        self.current_page = "dashboard"

        self.connection_var = tk.StringVar(value="Подключение…")
        self.status_var = tk.StringVar(value="Загружаю состояние сервера…")
        self.instance_var = tk.StringVar()
        self.notification_var = tk.StringVar(value="🔔 0")

        self._build_shell()
        self.bind_all("<Control-k>", lambda _event: self.open_palette())
        self.bind_all("<Control-K>", lambda _event: self.open_palette())
        self._ui_after = self.after(30, self._drain_ui_queue)
        self.after(50, self.sync)
        self.after(250, self._poll_power)
        self.after(400, self._poll_notifications)
        self.after(550, self._poll_events)

    def post_ui(self, callback: Callable[[], None]) -> None:
        self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        if self.closed:
            return
        for _index in range(500):
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except tk.TclError:
                pass
        self._ui_after = self.after(30, self._drain_ui_queue)

    def _build_shell(self) -> None:
        sidebar = ttk.Frame(self, style="Sidebar.TFrame", padding=(10, 14))
        sidebar.pack(side="left", fill="y")
        ttk.Label(sidebar, text="Server Control", style="SidebarTitle.TLabel").pack(anchor="w", padx=8, pady=(0, 4))
        ttk.Label(
            sidebar,
            text=f"{self.state.user.get('username')} · {self._role_label()}",
            style="SidebarSubtle.TLabel",
        ).pack(anchor="w", padx=8, pady=(0, 14))

        self.content = ttk.Frame(self)
        self.content.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(self.content, padding=(16, 10))
        header.pack(fill="x")
        self.page_title_var = tk.StringVar(value="Главная")
        ttk.Label(header, textvariable=self.page_title_var, font=("Segoe UI", 17, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.connection_var, style="Connection.TLabel").pack(side="left", padx=16)
        ttk.Button(header, textvariable=self.notification_var, command=lambda: self.select_page("logs")).pack(side="right")
        ttk.Button(header, text="Ctrl+K  Команды", command=self.open_palette).pack(side="right", padx=8)
        self.instance_box = ttk.Combobox(header, textvariable=self.instance_var, state="readonly", width=27)
        self.instance_box.pack(side="right", padx=8)
        self.instance_box.bind("<<ComboboxSelected>>", self._instance_selected)

        self.page_container = ttk.Frame(self.content)
        self.page_container.pack(fill="both", expand=True)
        footer = ttk.Frame(self.content, padding=(16, 6))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_var, style="Subtle.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="Выйти", command=self.logout_callback).pack(side="right")

        permissions = {
            "server": "server.view",
            "instances": "minecraft.view",
            "console": "minecraft.console",
            "files": "minecraft.files.read",
            "backups": "minecraft.backups",
            "monitoring": "server.view",
            "players": "minecraft.players",
            "users": "users.manage",
            "logs": "logs.view",
            "updates": "updates.manage",
            "settings": "settings.manage",
        }
        for page_type in self.PAGE_TYPES:
            if page_type.page_id in permissions and not self.state.has_permission(permissions[page_type.page_id]):
                continue
            page = page_type(self.page_container, self)
            self.pages[page.page_id] = page
            button = ttk.Button(sidebar, text=page.title, style="Nav.TButton", command=lambda name=page.page_id: self.select_page(name))
            button.pack(fill="x", pady=1)
            self._page_buttons[page.page_id] = button
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
        page.on_show()
        if name in {"console", "logs"}:
            self._schedule_aux("events_after", 50, self._poll_events)

    def _role_label(self) -> str:
        role = str(self.state.user.get("role"))
        if role in {"owner", "admin"}:
            return {"owner": "владелец", "admin": "администратор"}[role]
        permissions = set(self.state.user.get("permissions", []))
        presets = {
            "оператор": {"server.view", "minecraft.view", "minecraft.start", "minecraft.stop", "minecraft.restart", "minecraft.console", "minecraft.players", "logs.view"},
            "файловый менеджер": {"server.view", "minecraft.view", "minecraft.files.read", "minecraft.files.write", "logs.view"},
            "наблюдатель": {"server.view", "minecraft.view", "minecraft.files.read", "logs.view"},
        }
        return next((label for label, values in presets.items() if permissions == values), "индивидуальные права")

    def selected_instance_id(self) -> str | None:
        return self.state.selected_instance_id

    def agent_available(self, *, notify: bool = True, require_protocol: bool = True) -> bool:
        """Use one readiness decision for every operation executed by Agent."""

        if not self.state.server.get("online"):
            if notify:
                self.toast(
                    "Agent offline: операция не отправлена. После восстановления состояние обновится автоматически.",
                    error=True,
                )
            return False
        status = self.state.server.get("status") if isinstance(self.state.server.get("status"), dict) else {}
        if require_protocol and int(status.get("protocol_version", 1) or 1) < 2:
            if notify:
                self.toast("Эта функция требует Agent с протоколом 2. Откройте раздел «Обновления».", error=True)
            return False
        return True

    def _instance_selected(self, _event: tk.Event | None = None) -> None:
        label = self.instance_var.get()
        for instance_id, value in self.state.instances.items():
            if label == self._instance_label(value):
                if not self._prepare_instance_change(instance_id):
                    selected = self.state.selected_instance()
                    self.instance_var.set(self._instance_label(selected) if selected else "Сборки не найдены")
                    return
                self.state.selected_instance_id = instance_id
                break
        for page in self.pages.values():
            page.update_state()

    def select_instance(self, instance_id: str, page: str = "instances") -> None:
        if instance_id not in self.state.instances:
            return
        if not self._prepare_instance_change(instance_id):
            return
        self.state.selected_instance_id = instance_id
        self._refresh_instance_box()
        for value in self.pages.values():
            value.update_state()
        self.select_page(page)

    def _prepare_instance_change(self, instance_id: str) -> bool:
        if instance_id == self.state.selected_instance_id:
            return True
        files_page = self.pages.get("files")
        if files_page and hasattr(files_page, "prepare_instance_change"):
            return bool(files_page.prepare_instance_change())
        return True

    @staticmethod
    def _instance_label(value: dict[str, Any]) -> str:
        return f"{value.get('name', value.get('id', 'Minecraft'))}  ·  {value.get('state', 'UNKNOWN')}"

    def sync(self) -> None:
        if self.closed or self.sync_inflight:
            return
        self.sync_inflight = True
        started = time.monotonic()

        def work() -> dict[str, Any]:
            return self.api.request("GET", "/v1/server/status", timeout_seconds=8)

        def success(payload: dict[str, Any]) -> None:
            self.sync_inflight = False
            self.state.latency_ms = max(0, round((time.monotonic() - started) * 1000))
            self.state.apply_server_snapshot(
                payload,
                {"api": 2, "minimum_client": "1.0.0", "service": "server-control-hub"},
            )
            status = self.state.server.get("status") if isinstance(self.state.server.get("status"), dict) else {}
            agent_protocol = int(status.get("protocol_version", 1) or 1)
            if self.state.server.get("online") and agent_protocol < 2:
                self.connection_var.set("Нужно обновить Agent")
                self.status_var.set("Agent использует старый протокол; откройте раздел «Обновления»")
            else:
                age = self.state.server.get("age_ms")
                self.connection_var.set("Agent online" if self.state.server.get("online") else f"Agent offline · {int(age / 1000) if age else '—'} с")
            self._refresh_instance_box()
            for page in self.pages.values():
                page.update_state()
            self._schedule_sync(self._normal_sync_interval())

        def failure(error: Exception) -> None:
            self.sync_inflight = False
            self.state.mark_disconnected(error)
            self.connection_var.set("Нет связи · переподключение")
            for page in self.pages.values():
                page.update_state()
            delay = min(30_000, 1000 * (2 ** min(self.state.sync_failures - 1, 5)))
            self._schedule_sync(delay)
            if isinstance(error, ApiError) and error.code in {"access_revoked", "invalid_session", "authentication_required"}:
                messagebox.showerror("Доступ закрыт", error.message)
                self.logout_callback()

        self.run_async(work, success, failure, context="Статус сервера", quiet=True)

    def _schedule_sync(self, milliseconds: int) -> None:
        if self.closed:
            return
        if self.sync_after:
            try:
                self.after_cancel(self.sync_after)
            except tk.TclError:
                pass
        self.sync_after = self.after(milliseconds, self.sync)

    def _normal_sync_interval(self) -> int:
        """Keep the core feed responsive without coupling it to other data."""

        try:
            window_state = self.winfo_toplevel().state()
        except tk.TclError:
            window_state = "normal"
        if window_state in {"iconic", "withdrawn"}:
            return 15_000
        return 5_000

    def _schedule_aux(self, attribute: str, milliseconds: int, callback: Callable[[], None]) -> None:
        if self.closed:
            return
        current = getattr(self, attribute, None)
        if current:
            try:
                self.after_cancel(current)
            except tk.TclError:
                pass
        setattr(self, attribute, self.after(milliseconds, callback))

    def _poll_power(self) -> None:
        if self.closed:
            return
        if self.power_inflight or not self.state.has_permission("server.view"):
            self._schedule_aux("power_after", 15_000, self._poll_power)
            return
        self.power_inflight = True

        def success(payload: dict[str, Any]) -> None:
            self.power_inflight = False
            self.state.apply_power(payload)
            for page in self.pages.values():
                page.update_state()
            self._schedule_aux("power_after", 15_000, self._poll_power)

        def failure(_error: Exception) -> None:
            self.power_inflight = False
            self._schedule_aux("power_after", 30_000, self._poll_power)

        self.run_async(
            lambda: self.api.request("GET", "/v1/power/status", timeout_seconds=10),
            success,
            failure,
            context="Статус питания",
            quiet=True,
        )

    def _poll_events(self) -> None:
        if self.closed:
            return
        stream = "minecraft" if self.current_page == "console" else "server" if self.current_page == "logs" else None
        if stream is None:
            self._schedule_aux("events_after", 5_000, self._poll_events)
            return
        if self.events_inflight:
            self._schedule_aux("events_after", 1_000, self._poll_events)
            return
        self.events_inflight = True
        cursor = self.state.minecraft_event_cursor if stream == "minecraft" else self.state.server_event_cursor
        query = urllib.parse.urlencode({"after": cursor, "latest": 1 if cursor == 0 else 0})

        def success(payload: dict[str, Any]) -> None:
            self.events_inflight = False
            new_events = self.state.apply_events(payload, stream=stream)
            changes = {"events": new_events}
            for page in self.pages.values():
                page.update_state(changes)
            self._schedule_aux("events_after", 2_000 if stream == "minecraft" else 5_000, self._poll_events)

        def failure(_error: Exception) -> None:
            self.events_inflight = False
            self._schedule_aux("events_after", 5_000, self._poll_events)

        self.run_async(
            lambda: self.api.request("GET", f"/v1/{stream}/logs?{query}", timeout_seconds=8),
            success,
            failure,
            context="Журнал сервера",
            quiet=True,
        )

    def _poll_notifications(self) -> None:
        if self.closed:
            return
        if self.notifications_inflight:
            self._schedule_aux("notifications_after", 2_000, self._poll_notifications)
            return
        self.notifications_inflight = True
        query = urllib.parse.urlencode({"after": self.state.notification_cursor})

        def success(payload: dict[str, Any]) -> None:
            self.notifications_inflight = False
            new_notifications = self.state.apply_notifications(payload)
            unread = sum(1 for item in self.state.notifications if not item.get("is_read"))
            self.notification_var.set(f"🔔 {unread}")
            for page in self.pages.values():
                page.update_state({"notifications": new_notifications})
            for item in new_notifications:
                if item.get("severity") in {"warning", "error"}:
                    self.toast(str(item.get("message") or item.get("title")), error=item.get("severity") == "error")
            self._schedule_aux("notifications_after", 15_000, self._poll_notifications)

        def failure(_error: Exception) -> None:
            self.notifications_inflight = False
            self._schedule_aux("notifications_after", 30_000, self._poll_notifications)

        self.run_async(
            lambda: self.api.request("GET", f"/v1/notifications?{query}", timeout_seconds=8),
            success,
            failure,
            context="Уведомления",
            quiet=True,
        )

    def refresh_now(self) -> None:
        if self.sync_after:
            try:
                self.after_cancel(self.sync_after)
            except tk.TclError:
                pass
            self.sync_after = None
        if not self.sync_inflight:
            self.sync()
        self._schedule_aux("power_after", 20, self._poll_power)
        self._schedule_aux("events_after", 40, self._poll_events)
        self._schedule_aux("notifications_after", 60, self._poll_notifications)

    def _refresh_instance_box(self) -> None:
        labels = [self._instance_label(item) for item in self.state.instances.values()]
        self.instance_box.configure(values=labels)
        selected = self.state.selected_instance()
        if selected:
            self.instance_var.set(self._instance_label(selected))
        elif labels:
            self.instance_var.set(labels[0])
        else:
            self.instance_var.set("Сборки не найдены")

    def run_async(
        self,
        work: Callable[[], Any],
        success: Callable[[Any], None] | None = None,
        failure: Callable[[Exception], None] | None = None,
        *,
        context: str,
        quiet: bool = False,
    ) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as error:
                if not self.closed:
                    self.post_ui(lambda caught=error: failure(caught) if failure else self.handle_error(caught, context=context, quiet=quiet))
            else:
                if not self.closed and success:
                    self.post_ui(lambda completed=result: success(completed))

        threading.Thread(target=runner, daemon=True).start()

    def handle_error(self, error: Exception, *, context: str, quiet: bool = False) -> None:
        message = str(error)
        self.status(message)
        if not quiet:
            messagebox.showerror(context, message)

    def status(self, message: str, *, seconds: int = 8) -> None:
        self.status_var.set(message)
        if self._status_clear_after:
            try:
                self.after_cancel(self._status_clear_after)
            except tk.TclError:
                pass
        self._status_clear_after = self.after(seconds * 1000, lambda: self.status_var.set("Готово"))

    def toast(self, message: str, *, error: bool = False) -> None:
        self.status(f"{'Ошибка: ' if error else ''}{message}", seconds=12 if error else 6)

    def confirm(self, title: str, message: str, *, dangerous: bool = False) -> bool:
        return messagebox.askyesno(title, message, icon="warning" if dangerous else "question")

    def run_job(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        context: str,
        method: str = "POST",
        timeout: float = 180,
        success: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not self.agent_available():
            return
        self.status(f"{context}: поставлено в очередь…")
        self.preferences.remember("recent_actions", context)

        def progress(job: dict[str, Any]) -> None:
            snapshot = dict(job)
            self.post_ui(lambda: self.status(f"{context}: {snapshot.get('progress', 0)}% · {snapshot.get('message', '')}", seconds=30))

        def done(result: dict[str, Any]) -> None:
            self.status(f"{context}: выполнено")
            if success:
                success(result)
            self.refresh_now()

        self.run_async(
            lambda: self.api.run_job(path, payload, method=method, timeout_seconds=timeout, progress=progress),
            done,
            context=context,
        )

    def open_palette(self) -> None:
        actions: list[tuple[str, Callable[[], None]]] = [(f"Открыть: {page.title}", lambda name=page.page_id: self.select_page(name)) for page in self.pages.values()]
        instance = self.state.selected_instance()
        if instance:
            name = str(instance.get("name", "Minecraft"))
            if self.state.has_permission("minecraft.start"):
                actions.append((f"Запустить {name}", lambda: self.instance_action("start")))
            if self.state.has_permission("minecraft.stop"):
                actions.append((f"Остановить {name}", lambda: self.instance_action("stop")))
            if self.state.has_permission("minecraft.restart"):
                actions.append((f"Перезапустить {name}", lambda: self.instance_action("restart")))
            if self.state.has_permission("minecraft.console"):
                actions.append((f"Открыть консоль {name}", lambda: self.select_page("console")))
            if self.state.has_permission("minecraft.files.read"):
                actions.append((f"Открыть файлы {name}", lambda: self.select_page("files")))
            if self.state.has_permission("minecraft.backups"):
                actions.append((f"Создать backup {name}", lambda: self.create_backup()))
        for instance_id, item in self.state.instances.items():
            actions.append((f"Сборка: {item.get('name', instance_id)} — выбрать", lambda value=instance_id: self.select_instance(value)))
        status = self.state.server.get("status") if isinstance(self.state.server.get("status"), dict) else {}
        for backup in status.get("backups", []) if isinstance(status.get("backups"), list) else []:
            if isinstance(backup, dict):
                actions.append((f"Backup: {backup.get('instance_id')} · {backup.get('id')}", lambda: self.select_page("backups")))
        for item in self.preferences.get("recent_files", []):
            if not isinstance(item, str) or ":" not in item:
                continue
            instance_id, path = item.split(":", 1)
            files_page = self.pages.get("files")
            if files_page and hasattr(files_page, "open_path"):
                actions.append((f"Недавний файл: {path}", lambda value=instance_id, target=path, page=files_page: (self.select_instance(value, "files"), page.open_path(target))))
        for item in self.preferences.get("favourite_files", []):
            if not isinstance(item, str) or ":" not in item:
                continue
            instance_id, path = item.split(":", 1)
            files_page = self.pages.get("files")
            if files_page and hasattr(files_page, "open_path"):
                actions.append((f"★ Избранный файл: {path}", lambda value=instance_id, target=path, page=files_page: (self.select_instance(value, "files"), page.open_path(target))))
        for item in self.preferences.get("recent_actions", [])[:10]:
            actions.append((f"Недавнее действие: {item}", lambda: self.select_page("jobs")))
        actions.extend([
            ("Проверить обновления клиента", self.check_client_update),
            ("Обновить состояние", self.refresh_now),
        ])
        CommandPalette(self, actions)

    def instance_action(self, action: str) -> None:
        permission = {
            "start": "minecraft.start", "stop": "minecraft.stop",
            "restart": "minecraft.restart", "kill": "minecraft.kill",
        }.get(action)
        if not permission or not self.state.has_permission(permission):
            self.toast("Нет права для этого действия", error=True)
            return
        instance_id = self.selected_instance_id()
        if not instance_id:
            self.toast("Сборка не выбрана", error=True)
            return
        if not self.agent_available():
            return
        if action in {"stop", "restart", "kill"} and not self.confirm(
            "Подтвердите действие",
            "Принудительное завершение может повредить мир." if action == "kill" else f"{action.capitalize()} выбранную сборку?",
            dangerous=True,
        ):
            return
        self.run_job(
            f"/v1/instances/{urllib.parse.quote(instance_id, safe='')}/action",
            {"action": action},
            context=f"Minecraft: {action}",
            timeout=300,
        )

    def create_backup(self) -> None:
        if not self.state.has_permission("minecraft.backups"):
            self.toast("Нет права создания резервных копий", error=True)
            return
        instance_id = self.selected_instance_id()
        if instance_id:
            self.run_job("/v1/backups/action", {"action": "create", "instance_id": instance_id, "reason": "manual"}, context="Создание backup", timeout=24 * 60 * 60)

    def close(self) -> None:
        self.closed = True
        for timer in (self.sync_after, self.power_after, self.events_after, self.notifications_after):
            if timer:
                try:
                    self.after_cancel(timer)
                except tk.TclError:
                    pass
        if self._ui_after:
            try:
                self.after_cancel(self._ui_after)
            except tk.TclError:
                pass
        self.preferences.set("window_geometry", self.winfo_toplevel().geometry())
        console = self.pages.get("console")
        if console and hasattr(console, "command_input"):
            histories = dict(self.preferences.get("console_history", {}))
            instance_id = self.selected_instance_id()
            if instance_id:
                histories[instance_id] = console.command_input.history
                self.preferences.set("console_history", histories)
