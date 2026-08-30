"""Users, audit/logs, jobs, updates and application settings."""

from __future__ import annotations

import datetime as dt
import json
import tkinter as tk
import urllib.parse
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from pages_base import BasePage
from widgets import ConsoleView, enable_clipboard_paste


PERMISSION_LABELS = (
    ("server.view", "Просмотр сервера"), ("server.power", "Питание"), ("server.reboot", "Перезагрузка Linux"),
    ("server.services", "Службы"), ("server.processes", "Процессы"), ("minecraft.view", "Просмотр Minecraft"),
    ("minecraft.start", "Запуск"), ("minecraft.stop", "Остановка"), ("minecraft.restart", "Перезапуск"),
    ("minecraft.kill", "Force kill"), ("minecraft.console", "Консоль"), ("minecraft.players", "Игроки"),
    ("minecraft.instances.manage", "Создание сборок"), ("minecraft.settings", "Настройки сборок, server.properties и экспорт перевода"),
    ("minecraft.files.read", "Чтение файлов"), ("minecraft.files.write", "Изменение файлов"),
    ("minecraft.backups", "Создание backups"), ("minecraft.restore", "Восстановление"),
    ("minecraft.delete", "Удаление сборок"), ("logs.view", "Журналы"), ("audit.view", "Аудит"),
    ("users.manage", "Пользователи"), ("settings.manage", "Настройки"), ("updates.manage", "Обновления"),
)

PRESETS = {
    "Admin": {key for key, _label in PERMISSION_LABELS if key not in {"server.power", "minecraft.delete", "users.manage"}},
    "Operator": {"server.view", "minecraft.view", "minecraft.start", "minecraft.stop", "minecraft.restart", "minecraft.console", "minecraft.players", "logs.view"},
    "File Manager": {"server.view", "minecraft.view", "minecraft.files.read", "minecraft.files.write", "logs.view"},
    "Viewer": {"server.view", "minecraft.view", "minecraft.files.read", "logs.view"},
    "Custom": set(),
}


def _stamp(value: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000).strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"


class LogsPage(BasePage):
    page_id = "logs"
    title = "Журналы и уведомления"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.audit_after = 0
        self._seen_console_events: set[int] = set()
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        console_tab = ttk.Frame(tabs, padding=8)
        tabs.add(console_tab, text="Системный журнал")
        source_bar = ttk.Frame(console_tab)
        source_bar.pack(fill="x", pady=(0, 6))
        self.log_source = tk.StringVar(value="minecraft")
        ttk.Label(source_bar, text="Источник:").pack(side="left")
        ttk.Combobox(source_bar, textvariable=self.log_source, values=("minecraft", "crash", "service", "agent", "updater"), state="readonly", width=16).pack(side="left", padx=6)
        ttk.Button(source_bar, text="Загрузить последние строки", command=self.load_log).pack(side="left")
        ttk.Button(source_bar, text="Копировать показанное", command=lambda: self.console.copy_all()).pack(side="right")
        self.console = ConsoleView(console_tab)
        self.console.pack(fill="both", expand=True)
        audit_tab = ttk.Frame(tabs, padding=8)
        tabs.add(audit_tab, text="Аудит")
        toolbar = ttk.Frame(audit_tab)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="Обновить", command=self.refresh_audit).pack(side="left")
        self.audit_tree = ttk.Treeview(audit_tab, columns=("time", "user", "action", "target", "result", "details"), show="headings")
        for column, label, width in (("time", "Время", 145), ("user", "Пользователь", 120), ("action", "Действие", 220), ("target", "Объект", 150), ("result", "Результат", 100), ("details", "Подробности", 500)):
            self.audit_tree.heading(column, text=label)
            self.audit_tree.column(column, width=width, stretch=column == "details")
        self.audit_tree.pack(fill="both", expand=True)
        notice_tab = ttk.Frame(tabs, padding=8)
        tabs.add(notice_tab, text="Уведомления")
        ttk.Button(notice_tab, text="Отметить все прочитанными", command=self.mark_read).pack(anchor="e", pady=(0, 6))
        self.notice_tree = ttk.Treeview(notice_tab, columns=("time", "severity", "title", "message", "target"), show="headings")
        for column, label, width in (("time", "Время", 145), ("severity", "Уровень", 90), ("title", "Событие", 220), ("message", "Подробности", 500), ("target", "Объект", 150)):
            self.notice_tree.heading(column, text=label)
            self.notice_tree.column(column, width=width, stretch=column == "message")
        self.notice_tree.pack(fill="both", expand=True)

    def on_show(self) -> None:
        self.update_state({"events": self.panel.state.events})
        self.refresh_audit()

    def update_state(self, changes: dict[str, Any] | None = None) -> None:
        if changes:
            visible: list[dict[str, Any]] = []
            for event in changes.get("events", []):
                if event.get("kind") not in {"server", "audit"}:
                    continue
                try:
                    identifier = int(event.get("id"))
                except (TypeError, ValueError):
                    identifier = 0
                if identifier and identifier in self._seen_console_events:
                    continue
                if identifier:
                    self._seen_console_events.add(identifier)
                visible.append(event)
            if len(self._seen_console_events) > 15_000:
                self._seen_console_events = set(sorted(self._seen_console_events)[-10_000:])
            self.console.append(visible)
        self.notice_tree.delete(*self.notice_tree.get_children())
        for item in reversed(self.panel.state.notifications):
            marker = "" if item.get("is_read") else "● "
            self.notice_tree.insert("", "end", values=(_stamp(item.get("created_at")), item.get("severity"), marker + str(item.get("title", "")), item.get("message"), item.get("target")))

    def refresh_audit(self) -> None:
        if not self.panel.state.has_permission("audit.view"):
            return

        def success(result: dict[str, Any]) -> None:
            self.audit_tree.delete(*self.audit_tree.get_children())
            for item in result.get("events", []):
                details = json.dumps(item.get("details", {}), ensure_ascii=False, separators=(", ", ": "))
                self.audit_tree.insert("", "end", values=(_stamp(item.get("created_at")), item.get("username") or "system", item.get("action"), item.get("target") or "—", item.get("result") or "—", details))

        self.panel.run_async(lambda: self.panel.api.request("GET", "/v1/audit?after=0"), success, context="Аудит")

    def load_log(self) -> None:
        instance_id = self.panel.selected_instance_id()
        if not instance_id or not self.panel.agent_available():
            return
        source = self.log_source.get()

        def success(result: dict[str, Any]) -> None:
            events = [{"message": line, "kind": "server" if source in {"agent", "updater"} else "minecraft", "source": source} for line in result.get("lines", [])]
            self.console.clear_local()
            self.console.append(events)

        self.panel.run_async(lambda: self.panel.api.run_job("/v1/logs/read", {"instance_id": instance_id, "source": source, "limit": 1000}, timeout_seconds=60), success, context="Чтение журнала")

    def mark_read(self) -> None:
        ids = [int(item["id"]) for item in self.panel.state.notifications if not item.get("is_read") and item.get("id")]
        if not ids:
            return

        def success(_result: dict[str, Any]) -> None:
            for item in self.panel.state.notifications:
                if item.get("id") in ids:
                    item["is_read"] = 1
            self.update_state()

        self.panel.run_async(lambda: self.panel.api.request("POST", "/v1/notifications/read", {"ids": ids}), success, context="Уведомления")


class JobsPage(BasePage):
    page_id = "jobs"
    title = "Задачи и передачи"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="Обновить", command=self.refresh).pack(side="left")
        ttk.Button(toolbar, text="Отменить выбранную", command=self.cancel).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Отменить передачу", command=self.cancel_transfer).pack(side="left")
        self.tree = ttk.Treeview(self, columns=("type", "instance", "status", "progress", "stage", "message", "created"), show="headings", selectmode="browse")
        for column, label, width in (("type", "Операция", 180), ("instance", "Сборка", 110), ("status", "Статус", 100), ("progress", "Прогресс", 85), ("stage", "Этап", 120), ("message", "Сообщение", 420), ("created", "Создана", 145)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, stretch=column == "message")
        self.tree.pack(fill="both", expand=True)
        ttk.Label(self, text="Передачи", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 4))
        self.transfer_tree = ttk.Treeview(self, columns=("direction", "file", "instance", "size", "status", "expires"), show="headings", height=5)
        for column, label, width in (("direction", "Направление", 100), ("file", "Файл", 280), ("instance", "Сборка", 120), ("size", "Байт", 120), ("status", "Статус", 100), ("expires", "Истекает", 145)):
            self.transfer_tree.heading(column, text=label)
            self.transfer_tree.column(column, width=width, stretch=column == "file")
        self.transfer_tree.pack(fill="x")
        self.transfers: dict[str, dict[str, Any]] = {}
        self.detail = tk.Text(self, height=8, state="disabled", font=("Cascadia Mono", 9), wrap="word")
        self.detail.pack(fill="x", pady=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)

    def on_show(self) -> None:
        self.refresh()

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        selection = self.tree.selection()
        selected = selection[0] if selection else None
        self.tree.delete(*self.tree.get_children())
        for job_id, item in self.panel.state.jobs.items():
            self.tree.insert("", "end", iid=job_id, values=(item.get("type"), item.get("instance_id") or "—", item.get("status"), f"{item.get('progress', 0)}%", item.get("stage"), item.get("message"), _stamp(item.get("created_at"))))
        if selected in self.tree.get_children():
            self.tree.selection_set(selected)

    def refresh(self) -> None:
        def success(result: dict[str, Any]) -> None:
            for item in result.get("jobs", []):
                if isinstance(item, dict) and item.get("id"):
                    self.panel.state.jobs[str(item["id"])] = item
            self.update_state()

        suffix = "?all=1&limit=200" if self.panel.state.has_permission("audit.view") else "?limit=200"
        self.panel.run_async(lambda: self.panel.api.request("GET", f"/v1/jobs{suffix}"), success, context="Задачи")
        transfer_suffix = "?all=1" if self.panel.state.has_permission("audit.view") else ""

        def transfers_loaded(result: dict[str, Any]) -> None:
            self.transfers = {str(item["id"]): item for item in result.get("transfers", []) if isinstance(item, dict) and item.get("id")}
            self.transfer_tree.delete(*self.transfer_tree.get_children())
            for transfer_id, item in self.transfers.items():
                self.transfer_tree.insert("", "end", iid=transfer_id, values=(item.get("direction"), item.get("file_name"), item.get("instance_id"), item.get("size_bytes"), item.get("status"), _stamp(item.get("expires_at"))))

        self.panel.run_async(lambda: self.panel.api.request("GET", f"/v1/transfers{transfer_suffix}"), transfers_loaded, context="Передачи", quiet=True)

    def cancel(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        job_id = selection[0]
        self.panel.run_async(lambda: self.panel.api.request("POST", f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/cancel", {}), lambda _result: self.refresh(), context="Отмена задачи")

    def cancel_transfer(self) -> None:
        selection = self.transfer_tree.selection()
        if not selection:
            return
        transfer_id = selection[0]
        self.panel.run_async(lambda: self.panel.api.request("POST", f"/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/cancel", {}), lambda _result: self.refresh(), context="Отмена передачи")

    def show_detail(self, _event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        item = self.panel.state.jobs.get(selection[0]) if selection else None
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if item:
            self.detail.insert("1.0", json.dumps(item, ensure_ascii=False, indent=2))
        self.detail.configure(state="disabled")


class UsersPage(BasePage):
    page_id = "users"
    title = "Пользователи"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.users: dict[str, dict[str, Any]] = {}
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="Создать", command=lambda: self.user_dialog()).pack(side="left")
        ttk.Button(toolbar, text="Изменить права", command=self.edit_selected).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Включить / заблокировать", command=self.toggle).pack(side="left")
        ttk.Button(toolbar, text="Сбросить пароль", command=self.reset_password).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Отозвать все сеансы", command=self.revoke).pack(side="left")
        ttk.Button(toolbar, text="Удалить", style="Danger.TButton", command=self.delete).pack(side="right")
        self.tree = ttk.Treeview(self, columns=("role", "state", "permissions", "last_login"), show="tree headings")
        self.tree.heading("#0", text="Логин")
        self.tree.column("#0", width=180)
        for column, label, width in (("role", "Роль/набор", 130), ("state", "Доступ", 100), ("permissions", "Права", 600), ("last_login", "Последний вход", 145)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, stretch=column == "permissions")
        self.tree.pack(fill="both", expand=True)

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        def success(result: dict[str, Any]) -> None:
            self.users = {str(item["id"]): item for item in result.get("users", []) if isinstance(item, dict) and item.get("id")}
            self.tree.delete(*self.tree.get_children())
            for user_id, user in self.users.items():
                self.tree.insert("", "end", iid=user_id, text=user.get("username"), values=(user.get("role"), "включён" if user.get("enabled") else "заблокирован", ", ".join(user.get("permissions", [])), _stamp(user.get("last_login_at"))))

        self.panel.run_async(lambda: self.panel.api.request("GET", "/v1/admin/users"), success, context="Пользователи")

    def selected(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Пользователи", "Выберите пользователя.")
            return None
        return self.users.get(selection[0])

    def user_dialog(self, existing: dict[str, Any] | None = None) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Изменить пользователя" if existing else "Создать пользователя")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        username = tk.StringVar(value=str(existing.get("username", "")) if existing else "")
        password = tk.StringVar()
        preset = tk.StringVar(value="Custom" if existing else "Operator")
        ttk.Label(frame, text="Логин").grid(row=0, column=0, sticky="w", pady=4)
        user_entry = enable_clipboard_paste(ttk.Entry(frame, textvariable=username, state="disabled" if existing else "normal"))
        user_entry.grid(row=0, column=1, sticky="ew", pady=4)
        if not existing:
            ttk.Label(frame, text="Пароль (от 12 символов)").grid(row=1, column=0, sticky="w", pady=4)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=password, show="•")).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Набор прав").grid(row=2, column=0, sticky="w", pady=4)
        panel_user_is_owner = self.panel.state.user.get("role") == "owner"
        available_presets = tuple(PRESETS) if panel_user_is_owner else tuple(name for name in PRESETS if name != "Admin")
        if preset.get() not in available_presets:
            preset.set("Custom")
        preset_box = ttk.Combobox(frame, textvariable=preset, values=available_presets, state="readonly")
        preset_box.grid(row=2, column=1, sticky="ew", pady=4)
        permissions_frame = ttk.LabelFrame(frame, text="Точные разрешения", padding=8)
        permissions_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=8)
        current = set(existing.get("permissions", [])) if existing else PRESETS["Operator"]
        variables = {key: tk.BooleanVar(value=key in current) for key, _label in PERMISSION_LABELS}
        actor_permissions = set(self.panel.state.user.get("permissions", []))
        for index, (key, label) in enumerate(PERMISSION_LABELS):
            state = "normal" if panel_user_is_owner or key in actor_permissions else "disabled"
            ttk.Checkbutton(permissions_frame, text=label, variable=variables[key], state=state).grid(row=index % 12, column=index // 12, sticky="w", padx=6)

        def apply_preset(*_args: Any) -> None:
            desired = PRESETS[preset.get()]
            if preset.get() == "Custom":
                return
            for key, variable in variables.items():
                variable.set(key in desired)

        preset_box.bind("<<ComboboxSelected>>", apply_preset)

        def submit() -> None:
            selected_permissions = [key for key, variable in variables.items() if variable.get()]
            role = "admin" if panel_user_is_owner and preset.get() == "Admin" else "user"
            payload: dict[str, Any] = {"role": role, "permissions": selected_permissions}
            if not existing:
                payload.update({"username": username.get().strip(), "password": password.get()})
                method, path = "POST", "/v1/admin/users"
            else:
                method, path = "PATCH", f"/v1/admin/users/{urllib.parse.quote(str(existing['id']), safe='')}"
            dialog.destroy()
            self.panel.run_async(lambda: self.panel.api.request(method, path, payload), lambda _result: self.refresh(), context="Пользователь")

        ttk.Button(frame, text="Сохранить", command=submit).grid(row=4, column=1, sticky="e", pady=(8, 0))
        frame.columnconfigure(1, weight=1)

    def edit_selected(self) -> None:
        user = self.selected()
        if user and user.get("role") != "owner":
            self.user_dialog(user)

    def toggle(self) -> None:
        user = self.selected()
        if not user or user.get("role") == "owner":
            return
        enabled = not bool(user.get("enabled"))
        self.panel.run_async(lambda: self.panel.api.request("PATCH", f"/v1/admin/users/{urllib.parse.quote(str(user['id']), safe='')}", {"enabled": enabled}), lambda _result: self.refresh(), context="Доступ пользователя")

    def revoke(self) -> None:
        user = self.selected()
        if not user or user.get("role") == "owner":
            return
        self.panel.run_async(lambda: self.panel.api.request("POST", f"/v1/admin/users/{urllib.parse.quote(str(user['id']), safe='')}/revoke", {}), lambda _result: self.refresh(), context="Отзыв сеансов")

    def reset_password(self) -> None:
        user = self.selected()
        if not user:
            return
        password = simpledialog.askstring("Новый пароль", f"Новый пароль для {user.get('username')}", show="•", parent=self)
        if password:
            self.panel.run_async(lambda: self.panel.api.request("POST", f"/v1/admin/users/{urllib.parse.quote(str(user['id']), safe='')}/password", {"password": password}), lambda _result: self.panel.status("Пароль изменён; старые сеансы отозваны"), context="Смена пароля")

    def delete(self) -> None:
        user = self.selected()
        if not user or user.get("role") == "owner" or not self.panel.confirm("Удаление пользователя", f"Удалить {user.get('username')}?", dangerous=True):
            return
        self.panel.run_async(lambda: self.panel.api.request("DELETE", f"/v1/admin/users/{urllib.parse.quote(str(user['id']), safe='')}", {}), lambda _result: self.refresh(), context="Удаление пользователя")


class UpdatesPage(BasePage):
    page_id = "updates"
    title = "Обновления"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.health = tk.StringVar(value="Проверяю компоненты…")
        ttk.Label(self, textvariable=self.health, font=("Segoe UI", 11), wraplength=900).pack(anchor="w", pady=(0, 12))
        client = ttk.LabelFrame(self, text="Windows-приложение", padding=14)
        client.pack(fill="x")
        ttk.Label(client, text="Проверка GitHub Release, SHA/ZIP, отдельный updater, резервная копия предыдущего EXE и rollback при ошибке запуска.", style="Subtle.TLabel", wraplength=850).pack(side="left", fill="x", expand=True)
        ttk.Button(client, text="Проверить обновление", command=panel.check_client_update).pack(side="right")
        agent = ttk.LabelFrame(self, text="Agent на Debian", padding=14)
        agent.pack(fill="x", pady=12)
        ttk.Label(agent, text="Версия (latest или vX.Y.Z)").pack(side="left")
        self.version = tk.StringVar(value="latest")
        enable_clipboard_paste(ttk.Entry(agent, textvariable=self.version, width=18)).pack(side="left", padx=8)
        ttk.Button(agent, text="Обновить с health-check и rollback", command=self.update_agent).pack(side="left")
        self.output = tk.Text(self, height=14, state="disabled", font=("Cascadia Mono", 9), wrap="word")
        self.output.pack(fill="both", expand=True)

    def on_show(self) -> None:
        self.panel.run_async(lambda: self.panel.api.request("GET", "/v1/health"), self._health, context="Health check", quiet=True)

    def _health(self, result: dict[str, Any]) -> None:
        self.health.set(f"Backend: {'OK' if result.get('backend') else 'ERROR'} · D1: {'OK' if result.get('database') else 'ERROR'} · Agent: {'online' if result.get('agent') else 'offline'} · Agent {result.get('agent_version', '—')} · протокол {result.get('agent_protocol', '—')}")

    def update_agent(self) -> None:
        version = self.version.get().strip() or "latest"
        if not self.panel.confirm("Обновление Agent", f"Установить Agent {version}? Сервис проверит manifest, перезапустится и откатится при неуспешном health-check."):
            return

        def success(result: dict[str, Any]) -> None:
            self.output.configure(state="normal")
            self.output.delete("1.0", "end")
            self.output.insert("1.0", str(result.get("output") or json.dumps(result, ensure_ascii=False, indent=2)))
            self.output.configure(state="disabled")

        self.panel.run_job("/v1/updates/agent", {"version": version}, context="Обновление Agent", timeout=15 * 60, success=success)


class SettingsPage(BasePage):
    page_id = "settings"
    title = "Настройки"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.variables = {
            "console_retention_days": tk.StringVar(value="30"), "job_retention_days": tk.StringVar(value="30"),
            "notification_retention_days": tk.StringVar(value="90"), "backup_schedule_hours": tk.StringVar(value="24"),
            "restart_schedule_hours": tk.StringVar(value="0"), "disk_warning_percent": tk.StringVar(value="85"),
            "disk_critical_percent": tk.StringVar(value="95"),
        }
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        general = ttk.Frame(notebook, padding=16)
        automation = ttk.Frame(notebook, padding=16)
        appearance = ttk.Frame(notebook, padding=16)
        notebook.add(general, text="Хранение")
        notebook.add(automation, text="Автоматизация")
        notebook.add(appearance, text="Интерфейс")
        for row, (key, label) in enumerate((("console_retention_days", "Хранить консоль, дней"), ("job_retention_days", "Хранить задачи, дней"), ("notification_retention_days", "Хранить уведомления, дней"), ("disk_warning_percent", "Предупреждение диска, %"), ("disk_critical_percent", "Критический диск, %"))):
            ttk.Label(general, text=label).grid(row=row, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(general, textvariable=self.variables[key], width=12)).grid(row=row, column=1, sticky="w", padx=8)
        self.auto_cleanup = tk.BooleanVar(value=True)
        ttk.Checkbutton(general, text="Автоматически очищать старые журналы, задачи, уведомления и временные передачи", variable=self.auto_cleanup).grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        for row, (key, label) in enumerate((("backup_schedule_hours", "Backup всех активных профилей каждые N часов (0 — выкл.)"), ("restart_schedule_hours", "Плановый restart каждые N часов (0 — выкл.)"))):
            ttk.Label(automation, text=label).grid(row=row, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(automation, textvariable=self.variables[key], width=12)).grid(row=row, column=1, sticky="w", padx=8)
        ttk.Label(automation, text="Планировщик создаёт обычные отслеживаемые задачи и не запускает вторую операцию при активной блокировке.", style="Subtle.TLabel", wraplength=780).grid(row=3, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Label(appearance, text="Тема").grid(row=0, column=0, sticky="w")
        self.theme = tk.StringVar(value=str(panel.preferences.get("theme", "dark")))
        ttk.Combobox(appearance, textvariable=self.theme, values=("dark", "light"), state="readonly").grid(row=0, column=1, padx=8)
        ttk.Label(appearance, text="Изменение темы применяется после перезапуска клиента.", style="Subtle.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(self, text="Сохранить настройки", command=self.save).pack(anchor="e", pady=(10, 0))

    def on_show(self) -> None:
        self.panel.run_async(lambda: self.panel.api.request("GET", "/v1/settings"), self._loaded, context="Настройки")

    def _loaded(self, result: dict[str, Any]) -> None:
        settings = result.get("settings") if isinstance(result.get("settings"), dict) else {}
        for key, variable in self.variables.items():
            if key in settings:
                variable.set(str(settings[key]))
        if "auto_cleanup" in settings:
            self.auto_cleanup.set(bool(settings["auto_cleanup"]))

    def save(self) -> None:
        try:
            payload = {key: int(variable.get()) for key, variable in self.variables.items()}
        except ValueError:
            messagebox.showerror("Настройки", "Все числовые поля должны содержать целые числа.")
            return
        payload["auto_cleanup"] = self.auto_cleanup.get()
        self.panel.preferences.set("theme", self.theme.get())
        self.panel.run_async(lambda: self.panel.api.request("PATCH", "/v1/settings", payload), lambda _result: self.panel.status("Настройки сохранены"), context="Настройки")
