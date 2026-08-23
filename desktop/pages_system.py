"""Linux server, storage, processes, Java and service management pages."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from pages_base import BasePage
from widgets import ConsoleView, display_bytes, display_duration, enable_clipboard_paste


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _replace_tree(tree: ttk.Treeview, rows: list[tuple[str, tuple[Any, ...]]]) -> None:
    tree.delete(*tree.get_children())
    for identifier, values in rows:
        tree.insert("", "end", iid=str(identifier), values=values)


class ServerPage(BasePage):
    page_id = "server"
    title = "Сервер"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.summary = tk.StringVar(value="Ожидаю данные Linux…")
        ttk.Label(self, textvariable=self.summary, font=("Segoe UI", 11), wraplength=950).pack(anchor="w", pady=(0, 12))
        actions = ttk.LabelFrame(self, text="Питание и операционная система", padding=12)
        actions.pack(fill="x")
        if panel.state.has_permission("server.power"):
            ttk.Button(actions, text="Включить розетку", command=lambda: self._power("on")).pack(side="left")
            ttk.Button(actions, text="Безопасно выключить", style="Danger.TButton", command=lambda: self._power("off")).pack(side="left", padx=7)
            ttk.Button(actions, text="Выключить Linux", style="Danger.TButton", command=lambda: self._server_action("shutdown")).pack(side="left", padx=7)
        if panel.state.has_permission("server.reboot"):
            ttk.Button(actions, text="Перезагрузить Linux", command=lambda: self._server_action("reboot")).pack(side="left")

        self.command = tk.StringVar(value="uptime")
        if panel.state.user.get("role") == "owner" or "server_command" in panel.state.user.get("permissions", []):
            diagnostic = ttk.LabelFrame(self, text="Безопасная диагностика", padding=12)
            diagnostic.pack(fill="x", pady=12)
            box = enable_clipboard_paste(ttk.Combobox(
                diagnostic, textvariable=self.command,
                values=("uptime", "df -h", "free -h", "lsblk"),
            ))
            box.pack(side="left", fill="x", expand=True)
            ttk.Button(diagnostic, text="Выполнить", command=self._diagnostic).pack(side="left", padx=(8, 0))
            ttk.Label(self, text="Произвольный shell намеренно отключён. Доступны только точные команды из allow-list агента.", style="Subtle.TLabel").pack(anchor="w")
        self.console = ConsoleView(self)
        self.console.pack(fill="both", expand=True, pady=(10, 0))

    def update_state(self, changes: dict[str, Any] | None = None) -> None:
        status = _mapping(self.panel.state.server.get("status"))
        server = _mapping(status.get("server"))
        metrics = _mapping(server.get("metrics"))
        self.summary.set(
            f"{server.get('hostname', '—')} · аптайм {display_duration(metrics.get('uptime_seconds'))} · "
            f"агент {status.get('agent_version', '—')} · протокол {status.get('protocol_version', '—')} · "
            f"питание {'включено' if self.panel.state.power.get('on') is True else 'выключено' if self.panel.state.power.get('on') is False else 'неизвестно'}"
        )
        if changes:
            self.console.append([event for event in changes.get("events", []) if event.get("kind") == "server"])

    def _power(self, state: str) -> None:
        if not self.panel.state.has_permission("server.power"):
            self.panel.toast("Нет права управления питанием", error=True)
            return
        if state == "off":
            if not self.panel.agent_available():
                return
            if not self.panel.confirm("Безопасное выключение", "Сначала будут остановлены все Minecraft-сборки, затем Linux и только потом питание. Продолжить?", dangerous=True):
                return
        self.panel.run_async(
            lambda: self.panel.api.request("POST", "/v1/power/action", {"state": state}),
            lambda _result: self.panel.status("Команда питания принята"),
            context="Питание",
        )

    def _server_action(self, action: str) -> None:
        permission = "server.reboot" if action == "reboot" else "server.power"
        if not self.panel.state.has_permission(permission):
            self.panel.toast("Нет права для этого действия", error=True)
            return
        if not self.panel.agent_available():
            return
        if not self.panel.confirm("Linux-сервер", "Перезагрузить сервер?" if action == "reboot" else "Корректно выключить Linux-сервер?", dangerous=True):
            return
        self.panel.run_async(
            lambda: self.panel.api.request("POST", "/v1/server/action", {"action": action}),
            lambda _result: self.panel.status("Команда Linux поставлена в безопасную очередь"),
            context="Управление Linux",
        )

    def _diagnostic(self) -> None:
        value = self.command.get().strip()
        if not value:
            return
        if not self.panel.agent_available(require_protocol=False):
            return
        self.panel.run_async(
            lambda: self.panel.api.request("POST", "/v1/server/command", {"command": value}),
            lambda _result: self.panel.status("Диагностика поставлена в очередь"),
            context="Диагностика",
        )


class MonitoringPage(BasePage):
    page_id = "monitoring"
    title = "Мониторинг"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        self.storage_tree = self._tree_tab(tabs, "Диски", ("mount", "fs", "used", "free", "status"), (210, 110, 130, 130, 100))
        self.process_tree = self._tree_tab(tabs, "Процессы", ("pid", "name", "cpu", "ram", "runtime", "instance", "command"), (70, 150, 80, 110, 110, 130, 400))
        self.service_tree = self._tree_tab(tabs, "Службы", ("name", "description", "state", "pid"), (250, 400, 100, 80), actions=True)
        self.java_tree = self._tree_tab(tabs, "Java", ("version", "major", "vendor", "path"), (130, 70, 360, 430))
        self.category_tree = self._tree_tab(tabs, "Категории", ("category", "size", "detail"), (220, 150, 500))

    def _tree_tab(self, notebook: ttk.Notebook, label: str, columns: tuple[str, ...], widths: tuple[int, ...], actions: bool = False) -> ttk.Treeview:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text=label)
        if actions and self.panel.state.has_permission("server.services"):
            toolbar = ttk.Frame(frame)
            toolbar.pack(fill="x", pady=(0, 6))
            for action, title in (("start", "Запустить"), ("stop", "Остановить"), ("restart", "Перезапустить")):
                ttk.Button(toolbar, text=title, command=lambda value=action: self._service_action(value)).pack(side="left", padx=(0, 5))
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, width in zip(columns, widths):
            tree.heading(column, text={"mount": "Точка", "fs": "ФС", "used": "Занято", "free": "Свободно", "status": "Состояние", "pid": "PID", "name": "Имя", "cpu": "CPU", "ram": "RAM", "runtime": "Время", "instance": "Сборка", "command": "Команда", "description": "Описание", "state": "Статус", "version": "Версия", "major": "Major", "vendor": "Поставщик", "path": "Путь", "category": "Категория", "size": "Размер", "detail": "Подробности"}.get(column, column))
            tree.column(column, width=width, minwidth=50, stretch=column in {"command", "description", "path", "detail"})
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return tree

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        status = _mapping(self.panel.state.server.get("status"))
        storage = _mapping(status.get("storage"))
        mounts = storage.get("mounts") if isinstance(storage.get("mounts"), list) else []
        _replace_tree(self.storage_tree, [(str(index), (item.get("mountpoint"), item.get("filesystem"), display_bytes(item.get("used")), display_bytes(item.get("free")), item.get("warning", "ok"))) for index, item in enumerate(mounts) if isinstance(item, dict)])
        processes = status.get("processes") if isinstance(status.get("processes"), list) else []
        _replace_tree(self.process_tree, [(str(item.get("pid", index)), (item.get("pid"), item.get("name"), f"{item.get('cpu_percent', 0)}%", display_bytes(item.get("memory_bytes")), display_duration(item.get("runtime_seconds")), item.get("instance_id") or "—", item.get("command"))) for index, item in enumerate(processes) if isinstance(item, dict)])
        services = status.get("services") if isinstance(status.get("services"), list) else []
        _replace_tree(self.service_tree, [(str(index), (item.get("name"), item.get("description"), f"{item.get('active')}/{item.get('sub_state')}", item.get("pid"))) for index, item in enumerate(services) if isinstance(item, dict)])
        java = status.get("java") if isinstance(status.get("java"), list) else []
        _replace_tree(self.java_tree, [(str(index), (item.get("version"), item.get("major"), item.get("vendor"), item.get("path"))) for index, item in enumerate(java) if isinstance(item, dict)])
        categories = _mapping(storage.get("categories"))
        rows: list[tuple[str, tuple[Any, ...]]] = []
        for key in ("minecraft_total", "backups", "logs"):
            rows.append((key, (key, display_bytes(categories.get(key)), "")))
        for item in categories.get("instances", []) if isinstance(categories.get("instances"), list) else []:
            rows.append((f"instance-{item.get('instance_id')}", (f"Сборка: {item.get('name')}", ("≥ " if item.get("truncated") else "") + display_bytes(item.get("bytes")), f"файлов: {'≥ ' if item.get('truncated') else ''}{item.get('files', '—')}")))
        _replace_tree(self.category_tree, rows)

    def _service_action(self, action: str) -> None:
        if not self.panel.state.has_permission("server.services"):
            return
        selection = self.service_tree.selection()
        if not selection:
            messagebox.showinfo("Службы", "Выберите службу.")
            return
        service = str(self.service_tree.item(selection[0], "values")[0])
        if action in {"stop", "restart"} and not self.panel.confirm("Служба", f"{action.capitalize()} {service}?", dangerous=True):
            return
        self.panel.run_job("/v1/services/action", {"service": service, "action": action}, context=f"Служба {service}", timeout=180)
