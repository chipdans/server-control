"""Minecraft instances, live console and player management."""

from __future__ import annotations

import re
import shlex
import tkinter as tk
import urllib.parse
from tkinter import filedialog, messagebox, ttk
from typing import Any

from pages_base import BasePage, EmptyState
from widgets import ConsoleView, MinecraftCommandInput, TransferProgress, display_bytes, display_duration, enable_clipboard_paste, minecraft_completion_candidates


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _valid_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", value))


class InstancesPage(BasePage):
    page_id = "instances"
    title = "Сборки Minecraft"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 8))
        if panel.state.has_permission("minecraft.instances.manage"):
            ttk.Button(toolbar, text="＋ Добавить сервер", command=self._create_dialog).pack(side="left")
            ttk.Button(toolbar, text="Копировать", command=self._duplicate_dialog).pack(side="left", padx=6)
            ttk.Button(toolbar, text="Обновить из ZIP", command=self._update_files).pack(side="right", padx=6)
        if panel.state.has_permission("minecraft.settings"):
            ttk.Button(toolbar, text="Настройки", command=self._settings_dialog).pack(side="left", padx=6)
        if panel.state.has_permission("minecraft.delete"):
            ttk.Button(toolbar, text="Удалить", style="Danger.TButton", command=self._delete_dialog).pack(side="right")

        self.tree = ttk.Treeview(
            self,
            columns=("state", "version", "loader", "players", "ram", "cpu", "uptime", "size"),
            show="tree headings",
            selectmode="browse",
        )
        headings = {"#0": "Сборка", "state": "Статус", "version": "Minecraft", "loader": "Loader", "players": "Игроки", "ram": "RAM", "cpu": "CPU", "uptime": "Аптайм", "size": "Размер"}
        widths = {"#0": 230, "state": 100, "version": 100, "loader": 130, "players": 80, "ram": 105, "cpu": 70, "uptime": 100, "size": 100}
        for column, label in headings.items():
            self.tree.heading(column, text=label)
            self.tree.column(column, width=widths[column], minwidth=60, stretch=column == "#0")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.tree.bind("<Double-Button-1>", lambda _event: self.panel.select_page("console"))
        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(8, 0))
        action_permissions = {
            "start": "minecraft.start", "stop": "minecraft.stop",
            "restart": "minecraft.restart", "kill": "minecraft.kill",
        }
        for action, title in (("start", "Запустить"), ("stop", "Остановить"), ("restart", "Перезапустить"), ("kill", "Force kill")):
            if panel.state.has_permission(action_permissions[action]):
                ttk.Button(actions, text=title, style="Danger.TButton" if action == "kill" else "TButton", command=lambda value=action: panel.instance_action(value)).pack(side="left", padx=(0, 6))
        self.detail = tk.StringVar()
        ttk.Label(actions, textvariable=self.detail, style="Subtle.TLabel").pack(side="right")

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        selected = self.panel.state.selected_instance_id
        self.tree.delete(*self.tree.get_children())
        for instance_id, item in self.panel.state.instances.items():
            players = _mapping(item.get("players"))
            loader = " ".join(str(value) for value in (item.get("loader"), item.get("loader_version")) if value and value != "unknown") or "—"
            self.tree.insert(
                "", "end", iid=instance_id, text=str(item.get("name") or instance_id),
                values=(item.get("state", "UNKNOWN"), item.get("minecraft_version", "—"), loader, f"{players.get('online', '—')}/{players.get('max', '—')}", display_bytes(item.get("process_memory_bytes")), f"{item.get('process_cpu_percent', '—')}%", display_duration(item.get("uptime_seconds")), ("≥ " if item.get("size_truncated") else "") + display_bytes(item.get("size"))),
            )
        if selected in self.tree.get_children():
            self.tree.selection_set(selected)
        instance = self.panel.state.selected_instance()
        self.detail.set(f"{instance.get('directory', '')} · порт {instance.get('port', '—')} · Java {instance.get('java_version') or instance.get('java') or '—'}" if instance else "Сборки не найдены")

    def _select(self, _event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.panel.select_instance(selection[0], "instances")

    def _create_dialog(self) -> None:
        if not self.panel.agent_available():
            return
        dialog = tk.Toplevel(self)
        dialog.title("Добавить Minecraft-сервер")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.geometry("620x560")
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Новая сборка", font=("Segoe UI", 15, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        values: dict[str, tk.StringVar] = {
            "mode": tk.StringVar(value="Пустой профиль"), "instance_id": tk.StringVar(), "name": tk.StringVar(),
            "minecraft_version": tk.StringVar(value="latest"), "existing_path": tk.StringVar(),
            "ram_min_mb": tk.StringVar(value="2048"), "ram_max_mb": tk.StringVar(value="8192"),
            "port": tk.StringVar(value="25565"), "rcon_port": tk.StringVar(value="25575"), "zip": tk.StringVar(),
        }
        labels = {
            "empty": "Пустой профиль", "vanilla": "Установить Vanilla", "upload": "Загрузить ZIP",
            "import": "Импорт директории", "duplicate": "Копия выбранной сборки",
        }
        ttk.Label(frame, text="Способ").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=values["mode"], values=tuple(labels.values()), state="readonly").grid(row=1, column=1, sticky="ew", pady=4)
        row = 2
        for key, label in (("instance_id", "ID (a-z, 0-9, _ и -)"), ("name", "Название"), ("minecraft_version", "Версия Vanilla"), ("existing_path", "Относительный путь внутри /opt/minecraft"), ("ram_min_mb", "RAM MIN, МиБ"), ("ram_max_mb", "RAM MAX, МиБ"), ("port", "Порт игры"), ("rcon_port", "Порт RCON")):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=values[key])).grid(row=row, column=1, sticky="ew", pady=4)
            row += 1
        ttk.Label(frame, text="ZIP на этом компьютере").grid(row=row, column=0, sticky="w", pady=4)
        zip_row = ttk.Frame(frame)
        zip_row.grid(row=row, column=1, sticky="ew")
        enable_clipboard_paste(ttk.Entry(zip_row, textvariable=values["zip"])).pack(side="left", fill="x", expand=True)
        ttk.Button(zip_row, text="…", width=3, command=lambda: values["zip"].set(filedialog.askopenfilename(filetypes=(("ZIP", "*.zip"),)))) .pack(side="left", padx=(5, 0))
        row += 1
        eula = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Я принимаю Minecraft EULA (для Vanilla)", variable=eula).grid(row=row, column=0, columnspan=2, sticky="w", pady=8)
        row += 1
        auto_ports = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Автоматически подобрать свободные порты при конфликте", variable=auto_ports).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
        row += 1
        ttk.Label(frame, text="После импорта приложение определит loader, версию, JAR и startup-команду. Найденную команду нужно подтвердить перед первым запуском.", style="Subtle.TLabel", wraplength=570).grid(row=row, column=0, columnspan=2, sticky="w")

        def submit() -> None:
            instance_id = values["instance_id"].get().strip().lower()
            selected_mode = values["mode"].get()
            mode = next((key for key, label in labels.items() if label == selected_mode), "empty")
            if not _valid_id(instance_id):
                messagebox.showerror("Новая сборка", "Некорректный ID сборки.", parent=dialog)
                return
            try:
                payload: dict[str, Any] = {
                    "mode": mode, "instance_id": instance_id, "name": values["name"].get().strip() or instance_id,
                    "minecraft_version": values["minecraft_version"].get().strip() or "latest",
                    "existing_path": values["existing_path"].get().strip(), "ram_min_mb": int(values["ram_min_mb"].get()),
                    "ram_max_mb": int(values["ram_max_mb"].get()), "port": int(values["port"].get()),
                    "rcon_port": int(values["rcon_port"].get()), "accept_eula": eula.get(),
                    "auto_ports": auto_ports.get(),
                }
            except ValueError:
                messagebox.showerror("Новая сборка", "RAM и порты должны быть целыми числами.", parent=dialog)
                return
            if payload["ram_min_mb"] > payload["ram_max_mb"]:
                messagebox.showerror("Новая сборка", "RAM MIN не может быть больше RAM MAX.", parent=dialog)
                return
            if mode == "duplicate":
                source = self.panel.selected_instance_id()
                if not source:
                    messagebox.showerror("Новая сборка", "Сначала выберите исходную сборку.", parent=dialog)
                    return
                path = f"/v1/instances/{urllib.parse.quote(source, safe='')}/action"
                payload = {"action": "duplicate", "new_instance_id": instance_id, "name": payload["name"]}
            else:
                path = "/v1/instances"
            dialog.destroy()
            if mode == "upload":
                archive = values["zip"].get().strip()
                if not archive:
                    self.panel.toast("Для загрузки выберите ZIP", error=True)
                    return
                transfer_dialog = TransferProgress(self, "Импорт Minecraft ZIP")

                def work() -> dict[str, Any]:
                    transfer = self.panel.api.upload_staging_archive(archive, instance_id, progress=transfer_dialog.update_job, cancelled=transfer_dialog.cancelled, paused=transfer_dialog.paused)
                    transfer_data = transfer["transfer"]
                    payload["transfer_id"] = transfer_data["id"]
                    payload["transfer_sha256"] = transfer_data.get("sha256")
                    return self.panel.api.run_job(path, payload, timeout_seconds=24 * 60 * 60)

                def imported(_result: dict[str, Any]) -> None:
                    transfer_dialog.finish()
                    self.panel.status("Сборка импортирована")
                    self.panel.refresh_now()

                def failed(error: Exception) -> None:
                    transfer_dialog.finish()
                    self.panel.handle_error(error, context="Импорт ZIP")

                self.panel.run_async(work, imported, failed, context="Импорт ZIP")
            else:
                self.panel.run_job(path, payload, context="Создание сборки", timeout=24 * 60 * 60)

        ttk.Button(frame, text="Создать", command=submit).grid(row=row + 1, column=1, sticky="e", pady=(16, 0))
        frame.columnconfigure(1, weight=1)

    def _settings_dialog(self) -> None:
        instance = self.panel.state.selected_instance()
        if not instance:
            messagebox.showinfo("Настройки", "Выберите сборку.")
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"Настройки · {instance.get('name')}")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.geometry("720x650")
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        fields: dict[str, tk.StringVar] = {}
        definitions = (
            ("name", "Название"), ("java", "Путь к Java"), ("ram_min_mb", "RAM MIN, МиБ"),
            ("ram_max_mb", "RAM MAX, МиБ"), ("port", "Порт игры"), ("rcon_port", "Порт RCON"),
            ("jvm_arguments", "JVM-аргументы"), ("startup_arguments", "Аргументы сервера"),
            ("startup_command", "Базовая команда запуска"),
            ("shutdown_command", "Команда корректной остановки"),
            ("notes", "Заметки"), ("tags", "Теги через запятую"),
        )
        for row, (key, label) in enumerate(definitions):
            value = instance.get(key, "")
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value) if key == "tags" else shlex.join(str(item) for item in value)
            fields[key] = tk.StringVar(value=str(value or ""))
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=fields[key], width=48)).grid(row=row, column=1, sticky="ew", pady=4)
        reviewed = tk.BooleanVar(value=bool(instance.get("startup_reviewed")))
        favourite = tk.BooleanVar(value=bool(instance.get("favourite")))
        backup_before = tk.BooleanVar(value=bool(_mapping(instance.get("backup")).get("before_start")))
        ttk.Checkbutton(frame, text="Startup-команда проверена владельцем", variable=reviewed).grid(row=len(definitions), column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(frame, text="Избранная сборка", variable=favourite).grid(row=len(definitions) + 1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(frame, text="Создавать backup перед запуском", variable=backup_before).grid(row=len(definitions) + 2, column=0, columnspan=2, sticky="w", pady=4)

        def submit() -> None:
            try:
                jvm_arguments = shlex.split(fields["jvm_arguments"].get(), posix=True)
                startup_arguments = shlex.split(fields["startup_arguments"].get(), posix=True)
                startup_command = shlex.split(fields["startup_command"].get(), posix=True)
                payload = {
                    "name": fields["name"].get().strip(), "java": fields["java"].get().strip(),
                    "ram_min_mb": int(fields["ram_min_mb"].get()), "ram_max_mb": int(fields["ram_max_mb"].get()),
                    "port": int(fields["port"].get()), "rcon_port": int(fields["rcon_port"].get()),
                    "notes": fields["notes"].get().strip(), "tags": [item.strip() for item in fields["tags"].get().split(",") if item.strip()],
                    "jvm_arguments": jvm_arguments, "startup_arguments": startup_arguments, "startup_command": startup_command,
                    "shutdown_command": fields["shutdown_command"].get().strip().lstrip("/"),
                    "startup_reviewed": reviewed.get(), "favourite": favourite.get(),
                    "backup": {**_mapping(instance.get("backup")), "before_start": backup_before.get()},
                }
            except ValueError:
                messagebox.showerror("Настройки", "Проверьте RAM, порты и кавычки в аргументах запуска.", parent=dialog)
                return
            dialog.destroy()
            self.panel.run_job(f"/v1/instances/{urllib.parse.quote(str(instance['id']), safe='')}", payload, method="PATCH", context="Настройки сборки")

        ttk.Button(frame, text="Сохранить", command=submit).grid(row=len(definitions) + 3, column=1, sticky="e", pady=(14, 0))
        frame.columnconfigure(1, weight=1)

    def _duplicate_dialog(self) -> None:
        instance = self.panel.state.selected_instance()
        if not instance:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Копия сборки")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        identifier = tk.StringVar(value=f"{instance.get('id')}-copy")
        name = tk.StringVar(value=f"{instance.get('name')} — копия")
        for row, (label, variable) in enumerate((("Новый ID", identifier), ("Название", name))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=variable)).grid(row=row, column=1, sticky="ew", pady=5)

        def submit() -> None:
            if not _valid_id(identifier.get()):
                messagebox.showerror("Копия", "Некорректный ID.", parent=dialog)
                return
            dialog.destroy()
            self.panel.run_job(f"/v1/instances/{urllib.parse.quote(str(instance['id']), safe='')}/action", {"action": "duplicate", "new_instance_id": identifier.get(), "name": name.get()}, context="Копирование сборки", timeout=24 * 60 * 60)

        ttk.Button(frame, text="Копировать", command=submit).grid(row=2, column=1, sticky="e", pady=(12, 0))
        frame.columnconfigure(1, weight=1)

    def _update_files(self) -> None:
        if not self.panel.agent_available():
            return
        instance = self.panel.state.selected_instance()
        if not instance:
            return
        archive = filedialog.askopenfilename(parent=self, title="ZIP обновления сборки", filetypes=(("ZIP", "*.zip"),))
        if not archive:
            return
        if not self.panel.confirm(
            "Обновление сборки",
            "Будет создан safety backup. Если Minecraft запущен, он корректно остановится; файлы из ZIP будут наложены на staging-копию. При crash или timeout исходная директория вернётся автоматически. Продолжить?",
            dangerous=True,
        ):
            return
        transfer = TransferProgress(self, f"Обновление {instance.get('name')}")
        instance_id = str(instance["id"])

        def work() -> dict[str, Any]:
            staged = self.panel.api.upload_staging_archive(
                archive,
                instance_id,
                progress=transfer.update_job,
                cancelled=transfer.cancelled,
                paused=transfer.paused,
            )
            transfer_data = staged.get("transfer") if isinstance(staged.get("transfer"), dict) else {}
            return self.panel.api.run_job(
                f"/v1/instances/{urllib.parse.quote(instance_id, safe='')}/action",
                {
                    "action": "update_files",
                    "transfer_id": transfer_data.get("id"),
                    "transfer_sha256": transfer_data.get("sha256"),
                },
                timeout_seconds=24 * 60 * 60,
                progress=transfer.update_job,
                cancelled=transfer.cancelled,
            )

        def success(_result: dict[str, Any]) -> None:
            transfer.finish()
            self.panel.status("Сборка обновлена; safety backup сохранён")
            self.panel.refresh_now()

        def failure(error: Exception) -> None:
            transfer.finish()
            self.panel.handle_error(error, context="Обновление сборки")

        self.panel.run_async(work, success, failure, context="Обновление сборки")

    def _delete_dialog(self) -> None:
        instance = self.panel.state.selected_instance()
        if not instance:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Удаление сборки")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Сначала остановите сервер. Для подтверждения введите его ID:", wraplength=430).pack(anchor="w")
        confirmation = tk.StringVar()
        enable_clipboard_paste(ttk.Entry(frame, textvariable=confirmation)).pack(fill="x", pady=8)
        files = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Также безвозвратно удалить файлы сборки", variable=files).pack(anchor="w")

        def submit() -> None:
            if confirmation.get() != str(instance.get("id")):
                messagebox.showerror("Удаление", "ID не совпадает.", parent=dialog)
                return
            dialog.destroy()
            path = f"/v1/instances/{urllib.parse.quote(str(instance['id']), safe='')}"
            self.panel.run_job(path, {"confirm": confirmation.get(), "delete_files": files.get()}, method="DELETE", context="Удаление сборки", timeout=3600)

        ttk.Button(frame, text="Удалить", style="Danger.TButton", command=submit).pack(anchor="e", pady=(12, 0))


class ConsolePage(BasePage):
    page_id = "console"
    title = "Minecraft-консоль"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self._seen: set[int] = set()
        status = ttk.Frame(self)
        status.pack(fill="x", pady=(0, 8))
        self.startup_text = tk.StringVar(value="Minecraft offline")
        ttk.Label(status, textvariable=self.startup_text).pack(side="left")
        self.startup = ttk.Progressbar(status, maximum=100, length=360)
        self.startup.pack(side="right")
        quick = ttk.Frame(self)
        quick.pack(fill="x", pady=(0, 6))
        for command, label in (("save-all flush", "Save all"), ("whitelist on", "Whitelist on"), ("whitelist off", "Whitelist off"), ("weather clear", "Clear weather"), ("time set day", "Set day")):
            ttk.Button(quick, text=label, command=lambda value=command: self._submit(value)).pack(side="left", padx=(0, 5))
        ttk.Button(quick, text="Открыть журналы", command=lambda: panel.select_page("logs")).pack(side="right")
        ttk.Button(quick, text="Скопировать crash", command=self._copy_crash).pack(side="right", padx=5)
        self.console = ConsoleView(self)
        self.console.pack(fill="both", expand=True)
        histories = panel.preferences.get("console_history", {})
        history = histories.get(panel.selected_instance_id(), []) if isinstance(histories, dict) else []
        self.command_input = MinecraftCommandInput(self, candidates=self._candidates, submit=self._submit, history=history)
        self.command_input.pack(fill="x", pady=(8, 0))
        ttk.Label(self, text="Tab — подсказки · ↑/↓ — история · Enter — отправка · ответы помечаются [RCON]", style="Subtle.TLabel").pack(anchor="w", pady=(4, 0))

    def on_show(self) -> None:
        self.update_state({"events": self.panel.state.events})
        self.command_input.entry.focus_set()

    def update_state(self, changes: dict[str, Any] | None = None) -> None:
        instance = self.panel.state.selected_instance()
        if instance:
            startup = _mapping(instance.get("startup"))
            progress = int(startup.get("progress", 100 if instance.get("state") == "RUNNING" else 0) or 0)
            self.startup["value"] = max(0, min(100, progress))
            detail = startup.get("detail") or instance.get("service_state") or ""
            self.startup_text.set(f"{instance.get('state', 'UNKNOWN')} · {startup.get('label', '')} · {detail}")
        else:
            self.startup["value"] = 0
            self.startup_text.set("Сборка не выбрана")
        if not changes:
            return
        selected = self.panel.selected_instance_id()
        events: list[dict[str, Any]] = []
        for event in changes.get("events", []):
            if event.get("kind") != "minecraft" or event.get("instance_id") not in {None, selected}:
                continue
            try:
                identifier = int(event.get("id"))
            except (TypeError, ValueError):
                identifier = 0
            if identifier and identifier in self._seen:
                continue
            if identifier:
                self._seen.add(identifier)
            events.append(event)
        if len(self._seen) > 15_000:
            self._seen = set(sorted(self._seen)[-10_000:])
        self.console.append(events)

    def _candidates(self, value: str) -> list[str]:
        instance = self.panel.state.selected_instance() or {}
        players = _mapping(instance.get("players")).get("names", [])
        commands = instance.get("command_names", [])
        return minecraft_completion_candidates(value, commands if isinstance(commands, list) else [], players if isinstance(players, list) else [])

    def _submit(self, command: str) -> None:
        if not self.panel.state.has_permission("minecraft.console"):
            self.panel.toast("Нет права Minecraft-консоли", error=True)
            return
        if not self.panel.agent_available():
            return
        instance_id = self.panel.selected_instance_id()
        if not instance_id:
            self.panel.toast("Сборка не выбрана", error=True)
            return
        normalized = command.strip().lstrip("/")
        if not normalized:
            return
        self.console.append([{"message": f"▶ /{normalized}", "source": "client", "level": "INFO", "instance_id": instance_id}])

        def completed(result: dict[str, Any]) -> None:
            output = str(result.get("output") or "Команда выполнена.")
            lines = output.splitlines() or ["Команда выполнена."]
            events = [
                {
                    "message": f"[RCON] {line[:8000]}",
                    "source": "rcon",
                    "level": "INFO",
                    "instance_id": instance_id,
                }
                for line in lines[:250]
            ]
            if len(lines) > 250:
                events.append({
                    "message": f"[RCON] Вывод ограничен: скрыто строк {len(lines) - 250}",
                    "source": "rcon",
                    "level": "WARN",
                    "instance_id": instance_id,
                })
            self.console.append(events)

        self.panel.run_job(
            f"/v1/instances/{urllib.parse.quote(instance_id, safe='')}/command", {"command": normalized},
            context=f"/{normalized.split()[0]}", timeout=45, success=completed,
        )

    def _copy_crash(self) -> None:
        instance = self.panel.state.selected_instance() or {}
        crash = _mapping(instance.get("crash"))
        text = "\n".join(str(crash.get(key) or "") for key in ("summary", "solution", "evidence") if crash.get(key))
        if not text:
            self.panel.toast("Определённой причины crash пока нет")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.panel.status("Описание crash скопировано")


class PlayersPage(BasePage):
    page_id = "players"
    title = "Игроки"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.summary = tk.StringVar(value="Minecraft offline")
        ttk.Label(self, textvariable=self.summary).pack(anchor="w", pady=(0, 8))
        self.tree = ttk.Treeview(self, columns=("online", "whitelist", "op", "banned"), show="tree headings")
        self.tree.heading("#0", text="Игрок")
        for column, label in (("online", "Онлайн"), ("whitelist", "Whitelist"), ("op", "OP"), ("banned", "Ban")):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=110)
        self.tree.column("#0", width=280)
        self.tree.pack(fill="both", expand=True)
        form = ttk.Frame(self)
        form.pack(fill="x", pady=(8, 0))
        self.player = tk.StringVar()
        self.reason = tk.StringVar()
        self.coordinates = tk.StringVar(value="~ ~ ~")
        enable_clipboard_paste(ttk.Entry(form, textvariable=self.player, width=18)).pack(side="left")
        enable_clipboard_paste(ttk.Entry(form, textvariable=self.reason, width=28)).pack(side="left", padx=6)
        enable_clipboard_paste(ttk.Entry(form, textvariable=self.coordinates, width=16)).pack(side="left")
        for action, label in (("kick", "Kick"), ("ban", "Ban"), ("pardon", "Pardon"), ("whitelist_add", "+Whitelist"), ("whitelist_remove", "−Whitelist"), ("op", "OP"), ("deop", "De-OP"), ("teleport", "TP")):
            ttk.Button(form, text=label, command=lambda value=action: self._action(value)).pack(side="left", padx=(5, 0))
        self.tree.bind("<<TreeviewSelect>>", self._selection)

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        instance = self.panel.state.selected_instance() or {}
        players = _mapping(instance.get("players"))
        names = set(str(item) for item in players.get("names", []) if item)
        lists = _mapping(instance.get("player_lists"))
        whitelist = set(str(item) for item in lists.get("whitelist", []))
        ops = set(str(item) for item in lists.get("ops", []))
        banned = set(str(item) for item in lists.get("banned", []))
        all_names = sorted(names | whitelist | ops | banned, key=str.casefold)
        self.tree.delete(*self.tree.get_children())
        for index, name in enumerate(all_names):
            self.tree.insert("", "end", iid=str(index), text=name, values=("да" if name in names else "нет", "да" if name in whitelist else "нет", "да" if name in ops else "нет", "да" if name in banned else "нет"))
        self.summary.set(f"{instance.get('name', 'Minecraft')} · онлайн {players.get('online', '—')}/{players.get('max', '—')}")

    def _selection(self, _event: tk.Event | None = None) -> None:
        selected = self.tree.selection()
        if selected:
            self.player.set(str(self.tree.item(selected[0], "text")))

    def _action(self, action: str) -> None:
        instance_id = self.panel.selected_instance_id()
        name = self.player.get().strip()
        if not instance_id or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
            self.panel.toast("Укажите корректный ник игрока", error=True)
            return
        payload = {"instance_id": instance_id, "player": name, "action": action, "reason": self.reason.get().strip(), "coordinates": self.coordinates.get().strip()}
        self.panel.run_job("/v1/players/action", payload, context=f"Игрок {name}")
