#!/usr/bin/env python3
"""Windows desktop client for Server Control."""

from __future__ import annotations

import json
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from api import ApiClient, ApiError
from updater import download_update, is_newer, latest_release, launch_updater


APP_VERSION = "0.1.4"
APP_TITLE = "Server Control"
ALL_PERMISSIONS = [
    ("power_view", "Видеть питание"),
    ("power_control", "Управлять питанием"),
    ("server_view", "Видеть Linux-консоль"),
    ("server_command", "Отправлять Linux-команды"),
    ("minecraft_view", "Видеть консоль Minecraft"),
    ("minecraft_command", "Управлять Minecraft"),
]


class ConfigurationError(RuntimeError):
    pass


def application_directory() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def load_configuration() -> dict[str, Any]:
    path = application_directory() / "server-control.json"
    if not path.is_file():
        raise ConfigurationError(
            "Не найден файл server-control.json рядом с программой. "
            "Скопируйте server-control.json.example и укажите адрес Worker."
        )
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Не удалось прочитать server-control.json: {error}") from error
    url = config.get("api_base_url")
    if not isinstance(url, str) or not url.startswith("https://") or "YOUR-SUBDOMAIN" in url:
        raise ConfigurationError("В server-control.json укажите реальный HTTPS-адрес Cloudflare Worker.")
    return config


def enable_clipboard_paste(entry: ttk.Entry) -> ttk.Entry:
    """Make Windows paste shortcuts work even with a non-English keyboard layout."""

    def paste(event: tk.Event) -> str:
        try:
            value = event.widget.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            event.widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        event.widget.insert(tk.INSERT, value)
        return "break"

    def paste_from_ctrl(event: tk.Event) -> str | None:
        # On a Russian layout Tk can report Ctrl+V as Cyrillic "м".  Windows
        # still supplies virtual-key code 86, which is the physical V key.
        if event.keycode == 86 or str(event.keysym).lower() in {"v", "cyrillic_em"}:
            return paste(event)
        return None

    entry.bind("<<Paste>>", paste)
    entry.bind("<Control-KeyPress>", paste_from_ctrl, add="+")
    entry.bind("<Shift-Insert>", paste)
    return entry


class ServerControlApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_TITLE} {APP_VERSION}")
        self.root.geometry("1160x780")
        self.root.minsize(900, 620)
        self.root.option_add("*tearOff", False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.config: dict[str, Any] = {}
        self.api: ApiClient | None = None
        self.user: dict[str, Any] | None = None
        self.server_log_after = 0
        self.minecraft_log_after = 0
        self.server_logs_initialized = False
        self.minecraft_logs_initialized = False
        self.safe_power_off_pending = False
        self.polling = False
        self.closed = False
        self.user_cache: dict[str, dict[str, Any]] = {}

        self.status_var = tk.StringVar(value="Подготовка…")
        self.power_var = tk.StringVar(value="Питание сервера: неизвестно")
        self.server_state_var = tk.StringVar(value="Домашний сервер: неизвестно")
        self.minecraft_state_var = tk.StringVar(value="Minecraft: неизвестно")

        self._configure_style()
        try:
            self.config = load_configuration()
            self.api = ApiClient(str(self.config["api_base_url"]))
            self.show_login()
        except ConfigurationError as error:
            self.show_configuration_error(str(error))

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Subtle.TLabel", foreground="#5f6b7a")
        style.configure("Card.TLabelframe", padding=14)
        style.configure("Danger.TButton", foreground="#9b1c1c")

    def clear(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()

    def show_configuration_error(self, text: str) -> None:
        self.clear()
        frame = ttk.Frame(self.root, padding=40)
        frame.pack(expand=True, fill="both")
        ttk.Label(frame, text="Нужно настроить подключение", style="Title.TLabel").pack(anchor="w", pady=(0, 18))
        ttk.Label(frame, text=text, wraplength=700, justify="left").pack(anchor="w")
        ttk.Label(
            frame,
            text="После заполнения файла перезапустите программу.",
            style="Subtle.TLabel",
        ).pack(anchor="w", pady=(16, 0))

    def show_login(self) -> None:
        self.clear()
        frame = ttk.Frame(self.root, padding=40)
        frame.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(frame, text="Server Control", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Управление домашним сервером", style="Subtle.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 24)
        )
        ttk.Label(frame, text="Логин").grid(row=2, column=0, sticky="w", pady=4)
        self.login_username = enable_clipboard_paste(ttk.Entry(frame, width=34))
        self.login_username.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Пароль").grid(row=3, column=0, sticky="w", pady=4)
        self.login_password = enable_clipboard_paste(ttk.Entry(frame, width=34, show="•"))
        self.login_password.grid(row=3, column=1, sticky="ew", pady=4)
        self.login_password.bind("<Return>", lambda _event: self.login())
        self.login_button = ttk.Button(frame, text="Войти", command=self.login)
        self.login_button.grid(row=4, column=1, sticky="e", pady=(16, 4))
        ttk.Button(frame, text="Первичная настройка", command=self.show_bootstrap_dialog).grid(
            row=5, column=1, sticky="e", pady=(2, 0)
        )
        ttk.Label(frame, textvariable=self.status_var, style="Subtle.TLabel", wraplength=380).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(18, 0)
        )
        frame.columnconfigure(1, weight=1)
        self.login_username.focus_set()
        self.status_var.set(f"Версия клиента: {APP_VERSION}")
        self.root.after(800, self.check_for_updates)

    def show_bootstrap_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Первичная настройка владельца")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Создать единственный аккаунт владельца", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        ttk.Label(
            frame,
            text="Эта форма работает только до создания первого аккаунта. Ключ не сохраняется на компьютере.",
            wraplength=430,
            style="Subtle.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
        fields: list[tuple[str, bool]] = [("Ключ первоначальной настройки", True), ("Логин владельца", False), ("Пароль владельца", True)]
        entries: list[ttk.Entry] = []
        for row, (label, sensitive) in enumerate(fields, start=2):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = enable_clipboard_paste(ttk.Entry(frame, width=42, show="•" if sensitive else ""))
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            entries.append(entry)

        def submit() -> None:
            bootstrap_key, username, password = (entry.get() for entry in entries)
            self.async_call(
                lambda: self.require_api().setup_owner(bootstrap_key, username, password),
                lambda result: self._bootstrap_done(dialog, result),
                context="Создание владельца",
            )

        ttk.Button(frame, text="Создать владельца", command=submit).grid(row=5, column=1, sticky="e", pady=(16, 0))
        frame.columnconfigure(1, weight=1)
        entries[0].focus_set()

    def _bootstrap_done(self, dialog: tk.Toplevel, result: dict[str, Any]) -> None:
        dialog.destroy()
        self.enter_application(result)

    def login(self) -> None:
        username = self.login_username.get()
        password = self.login_password.get()
        if not username or not password:
            self.status_var.set("Введите логин и пароль.")
            return
        self.login_button.configure(state="disabled")
        self.status_var.set("Выполняется вход…")

        def success(result: dict[str, Any]) -> None:
            self.enter_application(result)

        def failure(error: Exception) -> None:
            self.login_button.configure(state="normal")
            self.status_var.set(str(error))

        self.async_call(lambda: self.require_api().login(username, password), success, failure, context="Вход")

    def enter_application(self, result: dict[str, Any]) -> None:
        self.user = dict(result["user"])
        self.server_log_after = 0
        self.minecraft_log_after = 0
        self.server_logs_initialized = False
        self.minecraft_logs_initialized = False
        self.clear()

        header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Server Control", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"{self.user['username']} · {self.role_label()}", style="Subtle.TLabel").pack(
            side="left", padx=16
        )
        ttk.Button(header, text="Выйти", command=self.logout).pack(side="right")

        overview = ttk.Frame(self.root, padding=(18, 4, 18, 10))
        overview.pack(fill="x")
        self.build_power_card(overview)
        self.build_status_card(overview)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self.build_server_tab()
        self.build_minecraft_tab()
        if self.has_permission("user_manage"):
            self.build_users_tab()

        footer = ttk.Label(self.root, textvariable=self.status_var, style="Subtle.TLabel", padding=(18, 0, 18, 10))
        footer.pack(fill="x")
        self.status_var.set("Подключено. Статус обновляется автоматически.")
        self.poll()

    def build_power_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Питание сервера", style="Card.TLabelframe")
        card.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Label(card, textvariable=self.power_var, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        button_row = ttk.Frame(card)
        button_row.pack(anchor="w", pady=(10, 0))
        if self.has_permission("power_control"):
            ttk.Button(button_row, text="Включить", command=lambda: self.power_action("on")).pack(side="left")
            ttk.Button(button_row, text="Безопасно выключить", command=lambda: self.power_action("off")).pack(side="left", padx=6)
            if self.user and self.user.get("role") == "owner":
                ttk.Button(button_row, text="Отключить сразу", style="Danger.TButton", command=self.force_power_off).pack(side="left")
        elif self.has_permission("power_view"):
            ttk.Label(card, text="У вас есть только просмотр состояния.", style="Subtle.TLabel").pack(anchor="w", pady=(8, 0))
        else:
            self.power_var.set("Питание сервера: доступ запрещён")

    def build_status_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Состояние", style="Card.TLabelframe")
        card.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Label(card, textvariable=self.server_state_var).pack(anchor="w")
        ttk.Label(card, textvariable=self.minecraft_state_var).pack(anchor="w", pady=(6, 0))

    def build_server_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Домашний сервер")
        if not self.has_permission("server_view"):
            ttk.Label(tab, text="У вас нет доступа к Linux-консоли.", style="Subtle.TLabel").pack(anchor="w")
            return

        controls = ttk.Frame(tab)
        controls.pack(fill="x", pady=(0, 8))
        if self.has_permission("server_command"):
            ttk.Button(controls, text="Статус", command=lambda: self.server_action("status")).pack(side="left")
            if self.user and self.user.get("role") == "owner":
                ttk.Button(controls, text="Бэкап", command=lambda: self.server_action("backup")).pack(side="left", padx=6)
                ttk.Button(controls, text="Перезагрузить", command=lambda: self.server_action("reboot")).pack(side="left", padx=6)
                ttk.Button(controls, text="Выключить Linux", command=lambda: self.server_action("shutdown")).pack(side="left", padx=6)
        self.server_console = self.console_widget(tab)
        if self.has_permission("server_command"):
            entry_row = ttk.Frame(tab)
            entry_row.pack(fill="x", pady=(8, 0))
            self.server_command_entry = enable_clipboard_paste(ttk.Entry(entry_row))
            self.server_command_entry.pack(side="left", fill="x", expand=True)
            self.server_command_entry.bind("<Return>", lambda _event: self.send_server_command())
            ttk.Button(entry_row, text="Отправить", command=self.send_server_command).pack(side="left", padx=(8, 0))
            ttk.Label(
                tab,
                text="Разрешены только команды из allow-list агента. Цепочки, sudo и перенаправления заблокированы.",
                style="Subtle.TLabel",
            ).pack(anchor="w", pady=(6, 0))

    def build_minecraft_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Minecraft")
        if not self.has_permission("minecraft_view"):
            ttk.Label(tab, text="У вас нет доступа к Minecraft-консоли.", style="Subtle.TLabel").pack(anchor="w")
            return

        controls = ttk.Frame(tab)
        controls.pack(fill="x", pady=(0, 8))
        if self.has_permission("minecraft_command"):
            for label, action in (("Запустить", "start"), ("Остановить", "stop"), ("Перезапустить", "restart"), ("Статус", "status")):
                ttk.Button(controls, text=label, command=lambda action=action: self.minecraft_action(action)).pack(side="left", padx=(0, 6))
        self.minecraft_console = self.console_widget(tab)
        if self.has_permission("minecraft_command"):
            entry_row = ttk.Frame(tab)
            entry_row.pack(fill="x", pady=(8, 0))
            self.minecraft_command_entry = enable_clipboard_paste(ttk.Entry(entry_row))
            self.minecraft_command_entry.pack(side="left", fill="x", expand=True)
            self.minecraft_command_entry.bind("<Return>", lambda _event: self.send_minecraft_command())
            ttk.Button(entry_row, text="Отправить", command=self.send_minecraft_command).pack(side="left", padx=(8, 0))

    def build_users_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Пользователи")
        controls = ttk.Frame(tab)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="Обновить", command=self.refresh_users).pack(side="left")
        ttk.Button(controls, text="Создать пользователя", command=self.show_create_user_dialog).pack(side="left", padx=6)
        ttk.Button(controls, text="Включить / отключить", command=self.toggle_selected_user).pack(side="left", padx=6)
        ttk.Button(controls, text="Сменить пароль", command=self.show_reset_password_dialog).pack(side="left", padx=6)
        columns = ("username", "role", "enabled", "permissions", "last_login")
        self.users_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        headings = {
            "username": "Логин",
            "role": "Роль",
            "enabled": "Доступ",
            "permissions": "Права",
            "last_login": "Последний вход",
        }
        widths = {"username": 160, "role": 90, "enabled": 100, "permissions": 420, "last_login": 150}
        for column in columns:
            self.users_tree.heading(column, text=headings[column])
            self.users_tree.column(column, width=widths[column], anchor="w")
        self.users_tree.pack(fill="both", expand=True)
        self.refresh_users()

    @staticmethod
    def console_widget(parent: ttk.Frame) -> tk.Text:
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)
        console = tk.Text(container, wrap="word", background="#101418", foreground="#d7e3ed", insertbackground="#ffffff")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=console.yview)
        console.configure(yscrollcommand=scrollbar.set, state="disabled")
        console.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return console

    def poll(self) -> None:
        if self.closed or not self.user or self.polling:
            if not self.closed:
                self.root.after(10_000, self.poll)
            return
        self.polling = True

        def work() -> dict[str, Any]:
            api = self.require_api()
            results: dict[str, Any] = {}
            if self.has_permission("power_view"):
                results["power"] = api.request("GET", "/v1/power/status")
            if self.has_permission("server_view") or self.has_permission("minecraft_view"):
                results["status"] = api.request("GET", "/v1/server/status")
            if self.has_permission("server_view"):
                latest = "&latest=1" if not self.server_logs_initialized else ""
                results["server_logs"] = api.request("GET", f"/v1/server/logs?after={self.server_log_after}{latest}")
            if self.has_permission("minecraft_view"):
                latest = "&latest=1" if not self.minecraft_logs_initialized else ""
                results["minecraft_logs"] = api.request(
                    "GET", f"/v1/minecraft/logs?after={self.minecraft_log_after}{latest}"
                )
            return results

        def success(results: dict[str, Any]) -> None:
            self.polling = False
            self.apply_poll_results(results)
            if not self.closed:
                self.root.after(10_000, self.poll)

        def failure(error: Exception) -> None:
            self.polling = False
            self.handle_error(error, quiet=True)
            if not self.closed:
                self.root.after(15_000, self.poll)

        self.async_call(work, success, failure, context="Обновление статуса")

    def apply_poll_results(self, results: dict[str, Any]) -> None:
        if "power" in results:
            power = results["power"].get("power", {})
            state = power.get("on")
            readable = "включено" if state is True else "выключено" if state is False else "не удалось определить"
            self.power_var.set(f"Питание сервера: {readable}")
            if state is False:
                self.safe_power_off_pending = False
        if "status" in results:
            status = results["status"]
            online = bool(status.get("online"))
            data = status.get("status") or {}
            server = data.get("server") or {}
            minecraft = data.get("minecraft") or {}
            self.server_state_var.set(f"Домашний сервер: {'онлайн' if online else 'нет связи'} · {server.get('hostname', '—')}")
            mc_state = "запущен" if minecraft.get("active") else "остановлен"
            self.minecraft_state_var.set(f"Minecraft: {mc_state}" if online else "Minecraft: нет связи с агентом")
        if "server_logs" in results:
            logs = results["server_logs"]
            self.server_log_after = int(logs.get("next_after", self.server_log_after))
            self.server_logs_initialized = True
            if hasattr(self, "server_console"):
                self.append_events(self.server_console, logs.get("events", []))
        if "minecraft_logs" in results:
            logs = results["minecraft_logs"]
            self.minecraft_log_after = int(logs.get("next_after", self.minecraft_log_after))
            self.minecraft_logs_initialized = True
            if hasattr(self, "minecraft_console"):
                self.append_events(self.minecraft_console, logs.get("events", []))

    @staticmethod
    def append_events(console: tk.Text, events: Any) -> None:
        if not isinstance(events, list) or not events:
            return
        console.configure(state="normal")
        for event in events:
            if isinstance(event, dict):
                console.insert("end", f"{event.get('message', '')}\n")
        line_count = int(console.index("end-1c").split(".")[0])
        if line_count > 1_500:
            console.delete("1.0", f"{line_count - 1_000}.0")
        console.see("end")
        console.configure(state="disabled")

    def power_action(self, state: str) -> None:
        if state == "on":
            self.command_request("POST", "/v1/power/action", {"state": state}, "Команда питания отправлена")
            return

        if self.safe_power_off_pending:
            messagebox.showinfo(
                "Безопасное выключение",
                "Оно уже запрошено. Не нажимайте кнопку повторно: Minecraft может останавливаться до трёх минут.",
            )
            return
        if not messagebox.askyesno(
            "Безопасное выключение",
            "Minecraft будет остановлен, данные синхронизированы, затем розетка отключит питание. Продолжить?",
        ):
            return

        self.safe_power_off_pending = True
        self.status_var.set("Безопасное выключение начато. Minecraft может останавливаться до трёх минут…")

        def success(result: dict[str, Any]) -> None:
            if result.get("already_pending"):
                self.status_var.set("Безопасное выключение уже выполняется. Повторная команда не создана.")
            else:
                self.status_var.set("Безопасное выключение начато. Не нажимайте кнопку повторно.")
            self.poll()

        def failure(error: Exception) -> None:
            self.safe_power_off_pending = False
            self.handle_error(error, context="Безопасное выключение")

        self.async_call(
            lambda: self.require_api().request("POST", "/v1/power/action", {"state": "off"}),
            success,
            failure,
            context="Безопасное выключение",
        )

    def force_power_off(self) -> None:
        if not messagebox.askyesno(
            "Принудительно отключить питание",
            "Это немедленно выключит розетку и может повредить данные Minecraft. Продолжить?",
            icon="warning",
        ):
            return
        self.command_request("POST", "/v1/power/action", {"state": "off", "force": True}, "Питание отключено")

    def server_action(self, action: str) -> None:
        confirmations = {
            "reboot": "Перезагрузить домашний сервер? Все активные процессы будут остановлены.",
            "shutdown": "Выключить Linux-сервер? После этого включить его можно только через умную розетку.",
        }
        if action in confirmations and not messagebox.askyesno("Подтвердите действие", confirmations[action], icon="warning"):
            return
        self.command_request("POST", "/v1/server/action", {"action": action}, "Команда сервера поставлена в очередь")

    def minecraft_action(self, action: str) -> None:
        if action in {"stop", "restart"} and not messagebox.askyesno(
            "Подтвердите действие", f"{action.capitalize()} Minecraft-сервер?", icon="warning"
        ):
            return
        self.command_request("POST", "/v1/minecraft/action", {"action": action}, "Команда Minecraft поставлена в очередь")

    def send_server_command(self) -> None:
        command = self.server_command_entry.get().strip()
        if not command:
            return
        self.server_command_entry.delete(0, "end")
        self.command_request("POST", "/v1/server/command", {"command": command}, "Linux-команда поставлена в очередь")

    def send_minecraft_command(self) -> None:
        command = self.minecraft_command_entry.get().strip()
        if not command:
            return
        self.minecraft_command_entry.delete(0, "end")
        self.command_request("POST", "/v1/minecraft/command", {"command": command}, "Команда Minecraft поставлена в очередь")

    def command_request(self, method: str, path: str, payload: dict[str, Any], success_message: str) -> None:
        self.status_var.set("Отправка команды…")

        def success(_result: dict[str, Any]) -> None:
            self.status_var.set(success_message)
            self.poll()

        self.async_call(lambda: self.require_api().request(method, path, payload), success, context=success_message)

    def refresh_users(self) -> None:
        if not self.has_permission("user_manage"):
            return

        def success(result: dict[str, Any]) -> None:
            users = result.get("users", [])
            self.user_cache = {str(user["id"]): dict(user) for user in users if isinstance(user, dict) and "id" in user}
            for child in self.users_tree.get_children():
                self.users_tree.delete(child)
            for user_id, user in self.user_cache.items():
                permissions = ", ".join(user.get("permissions", []))
                enabled = "включён" if user.get("enabled") else "отключён"
                last_login = self.format_timestamp(user.get("last_login_at"))
                self.users_tree.insert("", "end", iid=user_id, values=(user.get("username"), user.get("role"), enabled, permissions, last_login))

        self.async_call(lambda: self.require_api().request("GET", "/v1/admin/users"), success, context="Получение пользователей")

    def show_create_user_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Создать пользователя")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        username = self.labeled_entry(frame, 0, "Логин")
        password = self.labeled_entry(frame, 1, "Пароль (минимум 12 символов)", secret=True)
        ttk.Label(frame, text="Роль").grid(row=2, column=0, sticky="w", pady=4)
        role = tk.StringVar(value="user")
        role_box = ttk.Combobox(frame, textvariable=role, values=("user", "admin"), state="readonly", width=28)
        role_box.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Права").grid(row=3, column=0, sticky="nw", pady=4)
        permissions_frame = ttk.Frame(frame)
        permissions_frame.grid(row=3, column=1, sticky="w", pady=4)
        permission_values = {key: tk.BooleanVar(value=key in {"minecraft_view", "minecraft_command"}) for key, _ in ALL_PERMISSIONS}
        for row, (key, label) in enumerate(ALL_PERMISSIONS):
            ttk.Checkbutton(permissions_frame, text=label, variable=permission_values[key]).grid(row=row, column=0, sticky="w")

        def apply_role_defaults(*_args: Any) -> None:
            desired = {"minecraft_view", "minecraft_command"} if role.get() == "user" else {
                "power_view", "power_control", "server_view", "server_command", "minecraft_view", "minecraft_command"
            }
            for key, value in permission_values.items():
                value.set(key in desired)

        role_box.bind("<<ComboboxSelected>>", apply_role_defaults)

        def submit() -> None:
            permissions = [key for key, value in permission_values.items() if value.get()]
            payload = {"username": username.get(), "password": password.get(), "role": role.get(), "permissions": permissions}

            def done(_result: dict[str, Any]) -> None:
                dialog.destroy()
                self.refresh_users()
                self.status_var.set("Пользователь создан.")

            self.async_call(lambda: self.require_api().request("POST", "/v1/admin/users", payload), done, context="Создание пользователя")

        ttk.Button(frame, text="Создать", command=submit).grid(row=4, column=1, sticky="e", pady=(16, 0))
        frame.columnconfigure(1, weight=1)
        username.focus_set()

    def toggle_selected_user(self) -> None:
        user = self.selected_user()
        if not user:
            return
        if user.get("role") == "owner":
            messagebox.showinfo("Владелец", "Аккаунт владельца нельзя отключить из интерфейса.")
            return
        enabled = not bool(user.get("enabled"))
        verb = "включить" if enabled else "отключить"
        if not messagebox.askyesno("Подтвердите", f"{verb.capitalize()} доступ пользователю {user.get('username')}?"):
            return

        def done(_result: dict[str, Any]) -> None:
            self.refresh_users()
            self.status_var.set("Доступ пользователя изменён. Все старые сеансы отозваны.")

        self.async_call(
            lambda: self.require_api().request("PATCH", f"/v1/admin/users/{user['id']}", {"enabled": enabled}),
            done,
            context="Изменение доступа",
        )

    def show_reset_password_dialog(self) -> None:
        user = self.selected_user()
        if not user:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Сменить пароль")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Новый пароль для {user.get('username')}").grid(row=0, column=0, sticky="w")
        password = enable_clipboard_paste(ttk.Entry(frame, width=34, show="•"))
        password.grid(row=1, column=0, sticky="ew", pady=(8, 12))

        def submit() -> None:
            def done(_result: dict[str, Any]) -> None:
                dialog.destroy()
                self.status_var.set("Пароль изменён, старые сеансы отозваны.")

            self.async_call(
                lambda: self.require_api().request(
                    "POST", f"/v1/admin/users/{user['id']}/password", {"password": password.get()}
                ),
                done,
                context="Смена пароля",
            )

        ttk.Button(frame, text="Сменить", command=submit).grid(row=2, column=0, sticky="e")
        password.focus_set()

    @staticmethod
    def labeled_entry(parent: ttk.Frame, row: int, label: str, secret: bool = False) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        entry = enable_clipboard_paste(ttk.Entry(parent, width=34, show="•" if secret else ""))
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        return entry

    def selected_user(self) -> dict[str, Any] | None:
        selection = self.users_tree.selection() if hasattr(self, "users_tree") else ()
        if not selection:
            messagebox.showinfo("Пользователи", "Выберите пользователя в списке.")
            return None
        return self.user_cache.get(selection[0])

    def check_for_updates(self) -> None:
        update_config = self.config.get("update", {}) if isinstance(self.config.get("update"), dict) else {}
        if not update_config.get("enabled") or not getattr(sys, "frozen", False):
            return
        repository = str(update_config.get("repository", ""))
        asset_name = str(update_config.get("asset_name", "ServerControl-Update.zip"))
        automatic = bool(update_config.get("install_automatically", True))

        def work() -> dict[str, Any] | None:
            release = latest_release(repository, asset_name)
            if release and is_newer(str(release["tag"]), APP_VERSION):
                return release
            return None

        def success(release: dict[str, Any] | None) -> None:
            if not release or self.closed:
                return
            install = automatic or messagebox.askyesno(
                "Доступно обновление", f"Доступна версия {release['tag']}. Установить её сейчас?"
            )
            if not install:
                return
            self.status_var.set(f"Скачивание обновления {release['tag']}…")

            def apply() -> None:
                update_zip = download_update(str(release["url"]))
                launch_updater(update_zip, Path(sys.executable).resolve())

            def applied(_value: Any) -> None:
                self.status_var.set("Обновление скачано. Перезапуск…")
                self.root.after(500, self.close)

            self.async_call(apply, applied, context="Установка обновления")

        self.async_call(work, success, context="Проверка обновлений", quiet=True)

    def async_call(
        self,
        work: Callable[[], Any],
        success: Callable[[Any], None],
        failure: Callable[[Exception], None] | None = None,
        *,
        context: str,
        quiet: bool = False,
    ) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as error:  # return to Tk's main thread before touching UI
                if not self.closed:
                    def report(caught_error: Exception = error) -> None:
                        if failure:
                            failure(caught_error)
                        else:
                            self.handle_error(caught_error, context=context, quiet=quiet)

                    self.root.after(0, report)
            else:
                if not self.closed:
                    self.root.after(0, lambda: success(result))

        threading.Thread(target=runner, daemon=True).start()

    def handle_error(self, error: Exception, *, context: str = "", quiet: bool = False) -> None:
        if isinstance(error, ApiError) and error.code in {"access_revoked", "invalid_session", "authentication_required"}:
            if not quiet:
                messagebox.showerror("Доступ закрыт", error.message)
            self.logout(show_message=False)
            return
        message = str(error)
        self.status_var.set(message)
        if not quiet:
            messagebox.showerror(context or "Ошибка", message)

    def has_permission(self, permission: str) -> bool:
        return bool(self.user and permission in self.user.get("permissions", []))

    def require_api(self) -> ApiClient:
        if not self.api:
            raise RuntimeError("Клиент API не настроен")
        return self.api

    def role_label(self) -> str:
        roles = {"owner": "владелец", "admin": "администратор", "user": "пользователь"}
        return roles.get(str(self.user.get("role") if self.user else ""), "пользователь")

    @staticmethod
    def format_timestamp(value: Any) -> str:
        if not value:
            return "—"
        try:
            import datetime

            return datetime.datetime.fromtimestamp(int(value) / 1000).strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError, OSError):
            return "—"

    def logout(self, show_message: bool = True) -> None:
        if self.api:
            self.api.token = None
        self.user = None
        self.polling = False
        if show_message:
            self.status_var.set("Вы вышли из аккаунта.")
        self.show_login()

    def close(self) -> None:
        self.closed = True
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ServerControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
