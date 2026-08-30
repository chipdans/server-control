"""Simple direct-SSH Minecraft modpack library."""

from __future__ import annotations

import re
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from pages_base import BasePage
from local_translation import discover_client_instances
from widgets import TransferProgress, enable_clipboard_paste


INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
ONLINE_MODE_LABELS = {
    "Только лицензия Microsoft / Mojang": "true",
    "Без лицензии (offline-mode)": "false",
}
DIFFICULTY_LABELS = {"Мирная": "peaceful", "Лёгкая": "easy", "Нормальная": "normal", "Сложная": "hard"}
GAMEMODE_LABELS = {"Выживание": "survival", "Творческий": "creative", "Приключение": "adventure", "Наблюдатель": "spectator"}


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def state_text(value: str) -> str:
    return {
        "active": "Запущена",
        "activating": "Запускается",
        "deactivating": "Останавливается",
        "failed": "Ошибка",
        "inactive": "Остановлена",
        "unknown": "Неизвестно",
    }.get(value.casefold(), value or "Неизвестно")


def ram_text(value: Any) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return "—"
    return f"{amount / 1024:.1f} Gb" if amount >= 1024 else f"{amount} Mb"


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-")
    return result[:48] or f"server-{int(time.time())}"


def parse_server_properties(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            key, separator, value = line.partition(":")
        key = key.strip()
        if separator and key:
            values[key] = value.strip()
    return values


def update_server_properties(content: str, values: dict[str, str]) -> str:
    pending = {str(key): str(value).replace("\r", " ").replace("\n", " ") for key, value in values.items()}
    output: list[str] = []
    written: set[str] = set()
    for line in content.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = line.strip()
        key = ""
        if stripped and not stripped.startswith(("#", "!")):
            key = line.partition("=")[0].strip() if "=" in line else line.partition(":")[0].strip() if ":" in line else ""
        if key in pending:
            if key not in written:
                output.append(f"{key}={pending[key]}")
                written.add(key)
        else:
            output.append(line)
    if output and output[-1] and any(key not in written for key in pending):
        output.append("")
    for key, value in pending.items():
        if key not in written:
            output.append(f"{key}={value}")
    return "\n".join(output).rstrip("\n") + "\n"


class InstancesPage(BasePage):
    page_id = "instances"
    title = "Сборки Minecraft"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.instances: dict[str, dict[str, Any]] = {}
        self.active_id = ""
        self.selected_id = ""
        self.busy = False

        toolbar = ttk.Frame(self, style="Card.TFrame", padding=(13, 10))
        toolbar.pack(fill="x", pady=(0, 12))
        if panel.state.has_permission("minecraft.instances.manage"):
            ttk.Button(toolbar, text="＋  Добавить из ZIP", style="Accent.TButton", command=self.add_zip).pack(side="left")
            ttk.Button(toolbar, text="Импортировать папку", command=self.import_existing).pack(side="left", padx=8)
            ttk.Button(toolbar, text="Клонировать", command=self.clone).pack(side="left")
        ttk.Button(toolbar, text="↻  Обновить", command=self.refresh).pack(side="right")
        self.status_var = tk.StringVar(value="Открываю библиотеку сборок…")
        ttk.Label(toolbar, textvariable=self.status_var, style="SurfaceSubtle.TLabel").pack(side="right", padx=14)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        library = ttk.Frame(body, style="Card.TFrame", padding=12)
        library.pack(side="left", fill="both", expand=True, padx=(0, 7))
        ttk.Label(library, text="Установленные сборки", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 9))
        self.tree = ttk.Treeview(
            library,
            columns=("state", "minecraft", "loader", "ram", "port"),
            show="tree headings",
            selectmode="browse",
            height=15,
        )
        self.tree.heading("#0", text="Сборка")
        self.tree.column("#0", width=220, minwidth=170, stretch=True)
        for column, title, width in (
            ("state", "Состояние", 115),
            ("minecraft", "Minecraft", 90),
            ("loader", "Загрузчик", 105),
            ("ram", "RAM", 95),
            ("port", "Порт", 70),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=65, stretch=False)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-Button-1>", lambda _event: self.start())

        detail = ttk.Frame(body, style="Card.TFrame", padding=20, width=390)
        detail.pack(side="left", fill="y", padx=(7, 0))
        detail.pack_propagate(False)
        self.detail_name = tk.StringVar(value="Выберите сборку")
        self.detail_state = tk.StringVar(value="")
        self.detail_meta = tk.StringVar(value="")
        self.detail_ram = tk.StringVar(value="")
        self.detail_path = tk.StringVar(value="")
        self.detail_command = tk.StringVar(value="")
        ttk.Label(detail, textvariable=self.detail_name, style="CardValue.TLabel", wraplength=345).pack(anchor="w")
        self.state_label = ttk.Label(detail, textvariable=self.detail_state, style="StateNeutral.TLabel")
        self.state_label.pack(anchor="w", pady=(11, 4))
        ttk.Label(detail, textvariable=self.detail_meta, style="SurfaceSubtle.TLabel", wraplength=345).pack(anchor="w", pady=3)
        ttk.Label(detail, textvariable=self.detail_ram, style="SurfaceSubtle.TLabel", wraplength=345).pack(anchor="w", pady=3)
        ttk.Separator(detail).pack(fill="x", pady=15)
        ttk.Label(detail, text="Папка", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(detail, textvariable=self.detail_path, style="SurfaceSubtle.TLabel", wraplength=345).pack(anchor="w", pady=(4, 12))
        ttk.Label(detail, text="Команда запуска", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(detail, textvariable=self.detail_command, style="SurfaceSubtle.TLabel", wraplength=345).pack(anchor="w", pady=(4, 12))

        actions = ttk.Frame(detail, style="Surface.TFrame")
        actions.pack(side="bottom", fill="x")
        if panel.state.has_permission("minecraft.start"):
            ttk.Button(actions, text="▶  Запустить / переключить", style="Success.TButton", command=self.start).pack(fill="x", pady=3)
        row = ttk.Frame(actions, style="Surface.TFrame")
        row.pack(fill="x", pady=3)
        if panel.state.has_permission("minecraft.stop"):
            ttk.Button(row, text="■  Остановить", command=self.stop).pack(side="left", fill="x", expand=True, padx=(0, 3))
        if panel.state.has_permission("minecraft.restart"):
            ttk.Button(row, text="↻  Перезапустить", command=self.restart).pack(side="left", fill="x", expand=True, padx=(3, 0))
        if panel.state.has_permission("minecraft.settings"):
            ttk.Button(actions, text="Настройки сборки", command=self.settings).pack(fill="x", pady=3)
            ttk.Button(actions, text="⚙  Настройки сервера", style="Accent.TButton", command=self.server_settings).pack(fill="x", pady=3)
            ttk.Button(actions, text="🌐  Проверка перевода", command=self.export_translation).pack(fill="x", pady=3)
        if panel.state.has_permission("minecraft.delete"):
            ttk.Button(actions, text="Удалить сборку", style="Danger.TButton", command=self.delete).pack(fill="x", pady=3)

        note = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
        note.pack(fill="x", pady=(12, 0))
        ttk.Label(
            note,
            text="Одновременно работает одна сборка на основном адресе сервера. Миры, моды, настройки и логи каждой сборки хранятся отдельно.",
            style="SurfaceSubtle.TLabel",
        ).pack(anchor="w")

    def on_show(self) -> None:
        self.refresh()

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        active = self.instances.get(self.active_id)
        live = self.panel.state.instances.get(self.active_id) if self.active_id else None
        if active and isinstance(live, dict):
            state = str(live.get("service_state") or "active" if live.get("active") else "inactive")
            active["state"] = state
            self._render_tree(keep_selection=True)

    def _credentials(self) -> dict[str, Any]:
        return self.panel.api.terminal_credentials("linux")

    def _set_busy(self, value: bool, text: str = "") -> bool:
        if value and self.busy:
            return False
        self.busy = value
        if text:
            self.status_var.set(text)
        return True

    def refresh(self) -> None:
        if not self._set_busy(True, "Получаю список напрямую с сервера…"):
            return

        def success(result: dict[str, Any]) -> None:
            self.busy = False
            self._apply_result(result)
            self.status_var.set(f"Найдено сборок: {len(self.instances)}")

        def failure(error: Exception) -> None:
            self.busy = False
            self.status_var.set("Не удалось загрузить список")
            self.panel.status(str(error), error=True, seconds=20)

        self.panel.run_async(
            lambda: self.panel.direct_status.instance_request(self._credentials, {"action": "list"}),
            success,
            failure,
        )

    def _apply_result(self, result: dict[str, Any], *, select_id: str | None = None) -> None:
        values = result.get("instances") if isinstance(result.get("instances"), list) else []
        self.instances = {
            str(item["id"]): dict(item)
            for item in values
            if isinstance(item, dict) and item.get("id")
        }
        self.active_id = str(result.get("active_id") or "")
        if select_id in self.instances:
            self.selected_id = str(select_id)
        elif self.selected_id not in self.instances:
            self.selected_id = self.active_id if self.active_id in self.instances else next(iter(self.instances), "")
        self._render_tree()

    def _render_tree(self, *, keep_selection: bool = False) -> None:
        selected = self.selected_id if keep_selection else self.selected_id
        self.tree.delete(*self.tree.get_children())
        ordered = sorted(self.instances.values(), key=lambda item: (item.get("id") != self.active_id, str(item.get("name", "")).casefold()))
        for item in ordered:
            identifier = str(item["id"])
            active = identifier == self.active_id
            label = ("●  " if active else "◇  ") + str(item.get("name") or identifier)
            loader = str(item.get("loader") or "unknown")
            loader_version = str(item.get("loader_version") or "unknown")
            if loader_version not in {"", "unknown"}:
                loader += " " + loader_version
            self.tree.insert(
                "",
                "end",
                iid=identifier,
                text=label,
                values=(
                    state_text(str(item.get("state") or "inactive")),
                    item.get("minecraft_version") or "—",
                    loader if loader != "unknown" else "—",
                    ram_text(item.get("ram_max_mb")),
                    item.get("port") or "—",
                ),
            )
        if selected and selected in self.tree.get_children():
            self.tree.selection_set(selected)
            self.tree.see(selected)
        self._render_detail()

    def _selected(self, _event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if selection:
            self.selected_id = selection[0]
            self._render_detail()

    def selected(self) -> dict[str, Any] | None:
        return self.instances.get(self.selected_id)

    def _render_detail(self) -> None:
        item = self.selected()
        if not item:
            self.detail_name.set("Сборки не найдены")
            self.detail_state.set("Добавьте серверный ZIP или импортируйте папку")
            self.detail_meta.set("")
            self.detail_ram.set("")
            self.detail_path.set("")
            self.detail_command.set("")
            self.state_label.configure(style="StateNeutral.TLabel")
            return
        active = item["id"] == self.active_id
        state = str(item.get("state") or "inactive")
        self.detail_name.set(str(item.get("name") or item["id"]))
        self.detail_state.set(("Активная · " if active else "") + state_text(state))
        self.state_label.configure(
            style="StateSuccess.TLabel" if state == "active" else "StateDanger.TLabel" if state == "failed" else "StateNeutral.TLabel"
        )
        loader = str(item.get("loader") or "—")
        loader_version = str(item.get("loader_version") or "")
        self.detail_meta.set(
            f"Minecraft {item.get('minecraft_version') or '—'} · {loader} {loader_version if loader_version != 'unknown' else ''} · порт {item.get('port') or '—'}"
        )
        self.detail_ram.set(f"RAM: {ram_text(item.get('ram_min_mb'))} — {ram_text(item.get('ram_max_mb'))}")
        self.detail_path.set(str(item.get("directory") or "—"))
        command = item.get("startup_command") if isinstance(item.get("startup_command"), list) else []
        self.detail_command.set(" ".join(str(value) for value in command) or "—")

    def _run_operation(
        self,
        payload: dict[str, Any],
        text: str,
        success_text: str,
        *,
        timeout: int = 3600,
        select_id: str | None = None,
        done: Callable[[], None] | None = None,
    ) -> None:
        if not self._set_busy(True, text):
            return

        def success(result: dict[str, Any]) -> None:
            self.busy = False
            self._apply_result(result, select_id=select_id)
            self.status_var.set(success_text)
            self.panel.status(success_text, seconds=12)
            self.panel.after(700, self.panel.refresh_now)
            if done:
                done()

        def failure(error: Exception) -> None:
            self.busy = False
            self.status_var.set("Операция завершилась ошибкой")
            messagebox.showerror("Сборки Minecraft", str(error), parent=self)

        self.panel.run_async(
            lambda: self.panel.direct_status.instance_request(self._credentials, payload, timeout=timeout),
            success,
            failure,
        )

    def start(self) -> None:
        item = self.selected()
        if not item:
            return
        switching = bool(self.active_id and item["id"] != self.active_id)
        if switching and not messagebox.askyesno(
            "Переключить сборку?",
            f"Текущая сборка будет корректно остановлена, затем запустится «{item.get('name')}». Продолжить?",
            icon="warning",
            parent=self,
        ):
            return
        self._run_operation(
            {"action": "start", "id": item["id"]},
            "Переключаю и запускаю сборку…" if switching else "Запускаю сборку…",
            f"{item.get('name')} запускается",
            timeout=300,
            select_id=str(item["id"]),
        )

    def stop(self) -> None:
        item = self.selected()
        if not item or item["id"] != self.active_id:
            messagebox.showinfo("Остановка", "Эта сборка сейчас не активна.", parent=self)
            return
        if not messagebox.askyesno("Остановить Minecraft?", "Мир будет сохранён штатной командой stop.", icon="warning", parent=self):
            return
        self._run_operation({"action": "stop", "id": item["id"]}, "Останавливаю Minecraft…", "Minecraft остановлен", timeout=300)

    def restart(self) -> None:
        item = self.selected()
        if not item:
            return
        if not messagebox.askyesno("Перезапустить сборку?", f"Перезапустить «{item.get('name')}»?", icon="warning", parent=self):
            return
        self._run_operation({"action": "restart", "id": item["id"]}, "Перезапускаю сборку…", f"{item.get('name')} перезапускается", timeout=300)

    def add_zip(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Добавить серверную сборку")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.geometry("680x470")
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Новая сборка из ZIP", font=("Segoe UI Semibold", 18)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))
        archive = tk.StringVar()
        identifier = tk.StringVar()
        name = tk.StringVar()
        ram_min = tk.StringVar(value="2048")
        ram_max = tk.StringVar(value="8192")
        port = tk.StringVar(value="25565")
        eula = tk.BooleanVar(value=False)
        fields = (
            ("Серверный ZIP", archive),
            ("ID сборки", identifier),
            ("Название", name),
            ("Минимальная RAM, Mb", ram_min),
            ("Максимальная RAM, Mb", ram_max),
            ("Порт", port),
        )
        entries: list[ttk.Entry] = []
        for row, (label, variable) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            entry = enable_clipboard_paste(ttk.Entry(frame, textvariable=variable))
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            entries.append(entry)

        def browse() -> None:
            value = filedialog.askopenfilename(parent=dialog, title="Серверная сборка", filetypes=(("ZIP", "*.zip"),))
            if not value:
                return
            archive.set(value)
            stem = Path(value).stem
            if not name.get():
                name.set(stem[:80])
            if not identifier.get():
                identifier.set(slug(stem))

        ttk.Button(frame, text="Обзор…", command=browse).grid(row=1, column=2, padx=(7, 0))
        ttk.Checkbutton(frame, text="Я принимаю Minecraft EULA", variable=eula).grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 3))
        ttk.Label(
            frame,
            text="Поддерживаются готовые серверные ZIP с start.sh, run.sh, Forge/NeoForge args или server.jar.",
            style="Subtle.TLabel",
            wraplength=620,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 10))
        progress_var = tk.DoubleVar(value=0)
        progress = ttk.Progressbar(frame, variable=progress_var, maximum=100)
        progress.grid(row=9, column=0, columnspan=3, sticky="ew", pady=8)
        message = tk.StringVar()
        ttk.Label(frame, textvariable=message, style="Subtle.TLabel").grid(row=10, column=0, columnspan=3, sticky="w")
        submit_button = ttk.Button(frame, text="Загрузить и установить", style="Accent.TButton")
        submit_button.grid(row=11, column=2, sticky="e", pady=(12, 0))
        last_progress = [0.0]

        def submit() -> None:
            try:
                minimum, maximum, game_port = int(ram_min.get()), int(ram_max.get()), int(port.get())
            except ValueError:
                messagebox.showerror("Новая сборка", "RAM и порт должны быть целыми числами.", parent=dialog)
                return
            instance_id = identifier.get().strip().lower()
            if not Path(archive.get()).is_file() or Path(archive.get()).suffix.casefold() != ".zip":
                messagebox.showerror("Новая сборка", "Выберите серверный ZIP.", parent=dialog)
                return
            if not INSTANCE_ID_RE.fullmatch(instance_id):
                messagebox.showerror("Новая сборка", "ID: только a-z, 0-9, _ и -, до 48 символов.", parent=dialog)
                return
            if minimum < 256 or maximum < minimum:
                messagebox.showerror("Новая сборка", "Проверьте минимальную и максимальную RAM.", parent=dialog)
                return
            if not 1 <= game_port <= 65535:
                messagebox.showerror("Новая сборка", "Некорректный порт.", parent=dialog)
                return
            if not eula.get():
                messagebox.showerror("Новая сборка", "Нужно принять Minecraft EULA.", parent=dialog)
                return
            submit_button.configure(state="disabled")
            for entry in entries:
                entry.configure(state="disabled")
            message.set("Загружаю ZIP на сервер…")
            self.busy = True

            def upload_progress(current: int, total: int) -> None:
                now = time.monotonic()
                if current < total and now - last_progress[0] < 0.12:
                    return
                last_progress[0] = now
                percent = 100 * current / total if total else 0
                def update(value: float = percent) -> None:
                    progress_var.set(value)
                    message.set(f"Загрузка: {value:.1f}%")
                self.panel.post_ui(update)

            payload = {
                "id": instance_id,
                "name": name.get().strip() or instance_id,
                "ram_min_mb": minimum,
                "ram_max_mb": maximum,
                "port": game_port,
                "accept_eula": True,
            }

            def success(result: dict[str, Any]) -> None:
                self.busy = False
                dialog.destroy()
                self._apply_result(result, select_id=instance_id)
                self.status_var.set("Сборка установлена")
                self.panel.status("Сборка установлена и готова к запуску", seconds=15)

            def failure(error: Exception) -> None:
                self.busy = False
                submit_button.configure(state="normal")
                for entry in entries:
                    entry.configure(state="normal")
                message.set("Установка не завершена")
                messagebox.showerror("Установка сборки", str(error), parent=dialog)

            self.panel.run_async(
                lambda: self.panel.direct_status.import_instance_archive(
                    self._credentials, archive.get(), payload, progress=upload_progress
                ),
                success,
                failure,
            )

        submit_button.configure(command=submit)
        frame.columnconfigure(1, weight=1)

    def import_existing(self) -> None:
        self._profile_dialog("Импортировать папку", import_mode=True)

    def clone(self) -> None:
        item = self.selected()
        if not item:
            messagebox.showinfo("Клонирование", "Сначала выберите сборку.", parent=self)
            return
        self._profile_dialog("Клонировать сборку", source=item)

    def _profile_dialog(self, title: str, *, import_mode: bool = False, source: dict[str, Any] | None = None) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        identifier = tk.StringVar(value=(str(source.get("id")) + "-copy")[:48] if source else "")
        name = tk.StringVar(value=(str(source.get("name")) + " — копия")[:80] if source else "")
        directory = tk.StringVar(value="/opt/minecraft/" if import_mode else "")
        ram_min = tk.StringVar(value="2048")
        ram_max = tk.StringVar(value="8192")
        definitions: list[tuple[str, tk.StringVar]] = [("ID сборки", identifier), ("Название", name)]
        if import_mode:
            definitions.extend([("Папка на сервере", directory), ("Минимальная RAM, Mb", ram_min), ("Максимальная RAM, Mb", ram_max)])
        for row, (label, variable) in enumerate(definitions):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=variable, width=48)).grid(row=row, column=1, sticky="ew", pady=5)

        def submit() -> None:
            instance_id = identifier.get().strip().lower()
            if not INSTANCE_ID_RE.fullmatch(instance_id):
                messagebox.showerror(title, "Некорректный ID сборки.", parent=dialog)
                return
            if import_mode:
                try:
                    minimum, maximum = int(ram_min.get()), int(ram_max.get())
                except ValueError:
                    messagebox.showerror(title, "RAM должна быть целым числом.", parent=dialog)
                    return
                payload = {
                    "action": "import_existing",
                    "id": instance_id,
                    "name": name.get().strip() or instance_id,
                    "directory": directory.get().strip(),
                    "ram_min_mb": minimum,
                    "ram_max_mb": maximum,
                }
                operation_text, completed = "Проверяю и импортирую папку…", "Сборка импортирована"
            else:
                payload = {
                    "action": "clone",
                    "source_id": source["id"] if source else "",
                    "id": instance_id,
                    "name": name.get().strip() or instance_id,
                }
                operation_text, completed = "Копирую сборку…", "Копия создана"
            dialog.destroy()
            self._run_operation(payload, operation_text, completed, timeout=24 * 60 * 60, select_id=instance_id)

        ttk.Button(frame, text="Продолжить", style="Accent.TButton", command=submit).grid(row=len(definitions), column=1, sticky="e", pady=(14, 0))
        frame.columnconfigure(1, weight=1)

    def settings(self) -> None:
        item = self.selected()
        if not item:
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"Настройки · {item.get('name')}")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        name = tk.StringVar(value=str(item.get("name") or item["id"]))
        ram_min = tk.StringVar(value=str(item.get("ram_min_mb") or 2048))
        ram_max = tk.StringVar(value=str(item.get("ram_max_mb") or 8192))
        port = tk.StringVar(value=str(item.get("port") or 25565))
        redetect = tk.BooleanVar(value=False)
        for row, (label, variable) in enumerate((("Название", name), ("Минимальная RAM, Mb", ram_min), ("Максимальная RAM, Mb", ram_max), ("Порт", port))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=variable, width=38)).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Checkbutton(frame, text="Заново определить команду запуска и версию", variable=redetect).grid(row=4, column=0, columnspan=2, sticky="w", pady=8)

        def submit() -> None:
            try:
                minimum, maximum, game_port = int(ram_min.get()), int(ram_max.get()), int(port.get())
            except ValueError:
                messagebox.showerror("Настройки", "RAM и порт должны быть целыми числами.", parent=dialog)
                return
            if minimum < 256 or maximum < minimum or not 1 <= game_port <= 65535:
                messagebox.showerror("Настройки", "Проверьте значения RAM и порта.", parent=dialog)
                return
            dialog.destroy()
            self._run_operation(
                {
                    "action": "update", "id": item["id"], "name": name.get().strip(),
                    "ram_min_mb": minimum, "ram_max_mb": maximum, "port": game_port,
                    "redetect": redetect.get(),
                },
                "Сохраняю настройки…",
                "Настройки сохранены",
                select_id=str(item["id"]),
            )

        ttk.Button(frame, text="Сохранить", style="Accent.TButton", command=submit).grid(row=5, column=1, sticky="e", pady=(12, 0))
        frame.columnconfigure(1, weight=1)

    def server_settings(self) -> None:
        item = self.selected()
        if not item or not self._set_busy(True, "Читаю server.properties напрямую с сервера…"):
            return

        def failure(error: Exception) -> None:
            self.busy = False
            self.status_var.set("Не удалось открыть настройки сервера")
            messagebox.showerror("Настройки сервера", str(error), parent=self)

        def loaded(result: dict[str, Any]) -> None:
            self.busy = False
            self.status_var.set("Настройки сервера загружены")
            content = str(result.get("content") or "")
            original_sha256 = str(result.get("sha256") or "")
            properties = parse_server_properties(content)

            dialog = tk.Toplevel(self)
            dialog.title(f"Настройки сервера · {item.get('name')}")
            dialog.transient(self.winfo_toplevel())
            dialog.grab_set()
            dialog.geometry("980x760")
            dialog.minsize(860, 650)
            root = ttk.Frame(dialog, padding=18)
            root.pack(fill="both", expand=True)
            ttk.Label(root, text="Настройки Minecraft-сервера", font=("Segoe UI Semibold", 18)).pack(anchor="w")
            ttk.Label(
                root,
                text=f"Сборка: {item.get('name')} · {item.get('directory')}",
                style="Subtle.TLabel",
            ).pack(anchor="w", pady=(3, 12))

            reverse_online = {value: label for label, value in ONLINE_MODE_LABELS.items()}
            reverse_difficulty = {value: label for label, value in DIFFICULTY_LABELS.items()}
            reverse_gamemode = {value: label for label, value in GAMEMODE_LABELS.items()}
            online_mode = tk.StringVar(value=reverse_online.get(properties.get("online-mode", "true").casefold(), next(iter(ONLINE_MODE_LABELS))))
            difficulty = tk.StringVar(value=reverse_difficulty.get(properties.get("difficulty", "normal").casefold(), "Нормальная"))
            gamemode = tk.StringVar(value=reverse_gamemode.get(properties.get("gamemode", "survival").casefold(), "Выживание"))

            text_defaults = {
                "motd": "A Minecraft Server",
                "server-port": str(item.get("port") or 25565),
                "max-players": "20",
                "view-distance": "10",
                "simulation-distance": "10",
                "spawn-protection": "16",
                "level-name": "world",
                "level-seed": "",
                "player-idle-timeout": "0",
                "op-permission-level": "4",
                "max-world-size": "29999984",
                "network-compression-threshold": "256",
                "rate-limit": "0",
                "entity-broadcast-range-percentage": "100",
            }
            text_values = {key: tk.StringVar(value=properties.get(key, default)) for key, default in text_defaults.items()}
            bool_defaults = {
                "pvp": True,
                "allow-flight": False,
                "white-list": False,
                "enforce-whitelist": False,
                "enable-command-block": False,
                "hardcore": False,
                "force-gamemode": False,
                "enforce-secure-profile": True,
                "allow-nether": True,
                "spawn-monsters": True,
                "spawn-animals": True,
                "spawn-npcs": True,
                "enable-status": True,
                "hide-online-players": False,
            }
            bool_values = {
                key: tk.BooleanVar(value=str(properties.get(key, str(default))).casefold() == "true")
                for key, default in bool_defaults.items()
            }

            notebook = ttk.Notebook(root)
            notebook.pack(fill="both", expand=True)
            basic = ttk.Frame(notebook, padding=14)
            raw = ttk.Frame(notebook, padding=12)
            notebook.add(basic, text="Основные настройки")
            notebook.add(raw, text="Полный server.properties")
            basic.columnconfigure(0, weight=1)
            basic.columnconfigure(1, weight=1)

            access = ttk.LabelFrame(basic, text="Доступ и игровой процесс", padding=12)
            access.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
            world = ttk.LabelFrame(basic, text="Мир и подключение", padding=12)
            world.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
            access.columnconfigure(1, weight=1)
            world.columnconfigure(1, weight=1)

            def combo(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, choices: tuple[str, ...]) -> ttk.Combobox:
                ttk.Label(parent, text=label, style="Surface.TLabel").grid(row=row, column=0, sticky="w", pady=4)
                box = ttk.Combobox(parent, textvariable=variable, values=choices, state="readonly", width=31)
                box.grid(row=row, column=1, sticky="ew", pady=4)
                return box

            def entry(parent: ttk.Frame, row: int, label: str, key: str) -> None:
                ttk.Label(parent, text=label, style="Surface.TLabel").grid(row=row, column=0, sticky="w", pady=4)
                enable_clipboard_paste(ttk.Entry(parent, textvariable=text_values[key], width=31)).grid(row=row, column=1, sticky="ew", pady=4)

            combo(access, 0, "Проверка лицензии", online_mode, tuple(ONLINE_MODE_LABELS))
            combo(access, 1, "Сложность", difficulty, tuple(DIFFICULTY_LABELS))
            combo(access, 2, "Режим игры", gamemode, tuple(GAMEMODE_LABELS))
            entry(access, 3, "Максимум игроков", "max-players")
            combo(access, 4, "Уровень прав операторов", text_values["op-permission-level"], ("1", "2", "3", "4"))
            entry(access, 5, "AFK-таймаут, минут", "player-idle-timeout")
            access_checks = (
                ("pvp", "PvP между игроками"),
                ("allow-flight", "Разрешить полёт"),
                ("white-list", "Включить белый список"),
                ("enforce-whitelist", "Выгонять отсутствующих в белом списке"),
                ("enable-command-block", "Разрешить командные блоки"),
                ("hardcore", "Хардкорный режим"),
                ("force-gamemode", "Принудительно задавать режим игры"),
                ("enforce-secure-profile", "Требовать защищённый профиль"),
            )
            secure_check: ttk.Checkbutton | None = None
            for row, (key, label) in enumerate(access_checks, start=6):
                check = ttk.Checkbutton(access, text=label, variable=bool_values[key])
                check.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
                if key == "enforce-secure-profile":
                    secure_check = check

            entry(world, 0, "Название в списке (MOTD)", "motd")
            entry(world, 1, "Порт сервера", "server-port")
            entry(world, 2, "Дальность прорисовки", "view-distance")
            entry(world, 3, "Дальность симуляции", "simulation-distance")
            entry(world, 4, "Защита спавна", "spawn-protection")
            entry(world, 5, "Папка мира", "level-name")
            entry(world, 6, "Сид мира", "level-seed")
            entry(world, 7, "Максимальный размер мира", "max-world-size")
            entry(world, 8, "Сжатие сети", "network-compression-threshold")
            entry(world, 9, "Лимит пакетов", "rate-limit")
            entry(world, 10, "Дальность сущностей, %", "entity-broadcast-range-percentage")
            world_checks = (
                ("allow-nether", "Разрешить Нижний мир"),
                ("spawn-monsters", "Спавнить монстров"),
                ("spawn-animals", "Спавнить животных"),
                ("spawn-npcs", "Спавнить NPC"),
                ("enable-status", "Отвечать на запрос статуса"),
                ("hide-online-players", "Скрывать список игроков"),
            )
            for row, (key, label) in enumerate(world_checks, start=11):
                ttk.Checkbutton(world, text=label, variable=bool_values[key]).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)

            ttk.Label(
                raw,
                text="Здесь доступен весь файл без ограничений формы. Можно добавлять параметры модов и любые строки вручную.",
                style="Subtle.TLabel",
                wraplength=860,
            ).pack(anchor="w", pady=(0, 8))
            raw_toolbar = ttk.Frame(raw)
            raw_toolbar.pack(fill="x", pady=(0, 8))
            raw_body = ttk.Frame(raw)
            raw_body.pack(fill="both", expand=True)
            editor = enable_clipboard_paste(tk.Text(
                raw_body,
                wrap="none",
                undo=True,
                maxundo=200,
                background="#0d141a",
                foreground="#d7e1e9",
                insertbackground="#ffffff",
                selectbackground="#315a7d",
                font=("Cascadia Mono", 10),
            ))
            vertical = ttk.Scrollbar(raw_body, orient="vertical", command=editor.yview)
            horizontal = ttk.Scrollbar(raw_body, orient="horizontal", command=editor.xview)
            editor.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            editor.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
            raw_body.rowconfigure(0, weight=1)
            raw_body.columnconfigure(0, weight=1)
            editor.insert("1.0", content)

            def raw_content() -> str:
                return editor.get("1.0", "end-1c")

            def set_raw(value: str) -> None:
                editor.delete("1.0", "end")
                editor.insert("1.0", value)
                editor.edit_modified(False)

            def update_access_mode(*_args: object) -> None:
                offline = ONLINE_MODE_LABELS.get(online_mode.get()) == "false"
                if offline:
                    bool_values["enforce-secure-profile"].set(False)
                if secure_check is not None:
                    secure_check.configure(state="disabled" if offline else "normal")

            online_mode.trace_add("write", update_access_mode)
            update_access_mode()

            def form_values() -> dict[str, str]:
                numeric_limits = {
                    "server-port": (1, 65535),
                    "max-players": (1, 10000),
                    "view-distance": (2, 64),
                    "simulation-distance": (2, 64),
                    "spawn-protection": (0, 65535),
                    "player-idle-timeout": (0, 2147483647),
                    "op-permission-level": (1, 4),
                    "max-world-size": (1, 29999984),
                    "network-compression-threshold": (-1, 65535),
                    "rate-limit": (0, 2147483647),
                    "entity-broadcast-range-percentage": (10, 1000),
                }
                for key, (minimum, maximum) in numeric_limits.items():
                    try:
                        number = int(text_values[key].get().strip())
                    except ValueError as error:
                        raise ValueError(f"Поле «{key}» должно быть целым числом") from error
                    if not minimum <= number <= maximum:
                        raise ValueError(f"Поле «{key}»: допустимо от {minimum} до {maximum}")
                result_values = {key: variable.get().replace("\r", " ").replace("\n", " ") for key, variable in text_values.items()}
                result_values.update({
                    "online-mode": ONLINE_MODE_LABELS[online_mode.get()],
                    "difficulty": DIFFICULTY_LABELS[difficulty.get()],
                    "gamemode": GAMEMODE_LABELS[gamemode.get()],
                })
                result_values.update({key: "true" if variable.get() else "false" for key, variable in bool_values.items()})
                if result_values["online-mode"] == "false":
                    result_values["enforce-secure-profile"] = "false"
                return result_values

            def apply_form_to_text(*, show_error: bool = True) -> bool:
                try:
                    set_raw(update_server_properties(raw_content(), form_values()))
                except ValueError as error:
                    if show_error:
                        messagebox.showerror("Настройки сервера", str(error), parent=dialog)
                    return False
                return True

            def load_form_from_text() -> None:
                values = parse_server_properties(raw_content())
                if values.get("online-mode", "").casefold() in reverse_online:
                    online_mode.set(reverse_online[values["online-mode"].casefold()])
                if values.get("difficulty", "").casefold() in reverse_difficulty:
                    difficulty.set(reverse_difficulty[values["difficulty"].casefold()])
                if values.get("gamemode", "").casefold() in reverse_gamemode:
                    gamemode.set(reverse_gamemode[values["gamemode"].casefold()])
                for key, variable in text_values.items():
                    if key in values:
                        variable.set(values[key])
                for key, variable in bool_values.items():
                    if values.get(key, "").casefold() in {"true", "false"}:
                        variable.set(values[key].casefold() == "true")

            ttk.Button(raw_toolbar, text="Применить поля к тексту", command=apply_form_to_text).pack(side="left")
            ttk.Button(raw_toolbar, text="Прочитать поля из текста", command=load_form_from_text).pack(side="left", padx=7)
            ttk.Button(raw_toolbar, text="Вернуть исходный файл", command=lambda: set_raw(content)).pack(side="right")

            previous_tab = [0]
            changing_tab = [False]

            def tab_changed(_event: tk.Event | None = None) -> None:
                if changing_tab[0]:
                    return
                current = notebook.index(notebook.select())
                if previous_tab[0] == 0 and current == 1 and not apply_form_to_text():
                    changing_tab[0] = True
                    notebook.select(0)
                    changing_tab[0] = False
                    return
                if previous_tab[0] == 1 and current == 0:
                    load_form_from_text()
                previous_tab[0] = current

            notebook.bind("<<NotebookTabChanged>>", tab_changed)

            footer = ttk.Frame(root)
            footer.pack(fill="x", pady=(12, 0))
            ttk.Label(
                footer,
                text="Изменения применяются при следующем запуске Minecraft.",
                style="Subtle.TLabel",
            ).pack(side="left")
            service = mapping(result.get("service"))
            running = bool(result.get("active")) and str(service.get("active_state")) in {"active", "activating"}
            save_button = ttk.Button(footer, text="Сохранить", style="Accent.TButton")
            restart_button = ttk.Button(footer, text="Сохранить и перезапустить", style="Success.TButton")
            if running:
                restart_button.pack(side="right")
            save_button.pack(side="right", padx=(8, 8 if running else 0))

            def save(restart: bool) -> None:
                if notebook.index(notebook.select()) == 0 and not apply_form_to_text():
                    return
                updated = raw_content()
                if len(updated.encode("utf-8")) > 1024 * 1024:
                    messagebox.showerror("Настройки сервера", "server.properties больше допустимого размера 1 MB.", parent=dialog)
                    return
                if not self._set_busy(True, "Сохраняю server.properties…"):
                    return
                save_button.configure(state="disabled")
                restart_button.configure(state="disabled")
                payload = {
                    "action": "properties_set",
                    "id": item["id"],
                    "content": updated,
                    "expected_sha256": original_sha256,
                    "restart": restart,
                }

                def saved(response: dict[str, Any]) -> None:
                    self.busy = False
                    dialog.destroy()
                    self._apply_result(response, select_id=str(item["id"]))
                    message = "Настройки сохранены, Minecraft перезапускается" if response.get("restarted") else "Настройки сервера сохранены"
                    if response.get("restart_required"):
                        message += " · нужен перезапуск"
                    self.status_var.set(message)
                    self.panel.status(message, seconds=15)
                    self.panel.after(700, self.panel.refresh_now)

                def save_failed(error: Exception) -> None:
                    self.busy = False
                    save_button.configure(state="normal")
                    restart_button.configure(state="normal")
                    self.status_var.set("Не удалось сохранить настройки")
                    messagebox.showerror("Настройки сервера", str(error), parent=dialog)

                self.panel.run_async(
                    lambda: self.panel.direct_status.instance_request(self._credentials, payload, timeout=300),
                    saved,
                    save_failed,
                )

            save_button.configure(command=lambda: save(False))
            restart_button.configure(command=lambda: save(True))

        self.panel.run_async(
            lambda: self.panel.direct_status.instance_request(self._credentials, {"action": "properties_get", "id": item["id"]}),
            loaded,
            failure,
        )

    def export_translation(self) -> None:
        item = self.selected()
        if not item:
            return
        saved_paths = self.panel.preferences.get("translation_client_paths", {})
        saved = str(saved_paths.get(str(item["id"]), "")) if isinstance(saved_paths, dict) else ""
        discovered = discover_client_instances()
        choices = [str(path) for path in discovered]
        if saved and saved not in choices:
            choices.insert(0, saved)

        dialog = tk.Toplevel(self)
        dialog.title("Клиентская сборка для проверки")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.geometry("720x285")
        frame = ttk.Frame(dialog, style="Card.TFrame", padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Выберите папку клиентской сборки", style="CardValue.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="Моды и русификаторы будут проверены на этом компьютере. Квесты приложение возьмёт с выбранной серверной сборки.",
            style="SurfaceSubtle.TLabel",
            wraplength=660,
        ).pack(anchor="w", pady=(6, 14))
        client_path = tk.StringVar(value=saved or (choices[0] if choices else ""))
        path_row = ttk.Frame(frame, style="Surface.TFrame")
        path_row.pack(fill="x")
        path_box = ttk.Combobox(path_row, textvariable=client_path, values=choices)
        path_box.pack(side="left", fill="x", expand=True)

        def browse() -> None:
            selected = filedialog.askdirectory(
                parent=dialog,
                title="Корень клиентской сборки Minecraft",
                initialdir=client_path.get() or None,
            )
            if selected:
                client_path.set(selected)

        ttk.Button(path_row, text="Обзор…", command=browse).pack(side="left", padx=(8, 0))
        detected_text = (
            f"Автоматически найдено профилей: {len(choices)}. Учитываются встроенные переводы, KubeJS, OpenLoader и активные ресурспаки."
            if choices else
            "Профили автоматически не найдены. Нажмите «Обзор» и выберите папку, внутри которой находится каталог mods."
        )
        ttk.Label(frame, text=detected_text, style="SurfaceSubtle.TLabel", wraplength=660).pack(anchor="w", pady=(12, 0))
        footer = ttk.Frame(frame, style="Surface.TFrame")
        footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="Отмена", command=dialog.destroy).pack(side="right")

        def submit() -> None:
            try:
                root = Path(client_path.get()).expanduser().resolve(strict=True)
            except OSError as error:
                messagebox.showerror("Клиентская сборка", f"Папка недоступна:\n{error}", parent=dialog)
                return
            if not (root / "mods").is_dir():
                messagebox.showerror(
                    "Клиентская сборка",
                    "В выбранной папке нет каталога mods. Выберите корень профиля Minecraft.",
                    parent=dialog,
                )
                return
            values = dict(saved_paths) if isinstance(saved_paths, dict) else {}
            values[str(item["id"])] = str(root)
            self.panel.preferences.set("translation_client_paths", values)
            dialog.destroy()
            self._run_translation_export(item, root)

        ttk.Button(footer, text="Продолжить", style="Accent.TButton", command=submit).pack(side="right", padx=(0, 8))

    def _run_translation_export(self, item: dict[str, Any], client_directory: Path) -> None:
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить материалы для перевода",
            initialfile=f"{item['id']}-translation-export.zip",
            defaultextension=".zip",
            filetypes=(("ZIP", "*.zip"),),
        )
        if not destination or not self._set_busy(True, "Сканирую переводы модов и квестов…"):
            return
        transfer = TransferProgress(self, f"Проверка перевода · {item.get('name')}")
        transfer.update_job({
            "stage": "scan",
            "progress": 5,
            "message": "Получаю актуальные квесты с сервера…",
        })

        def download_progress(current: int, total: int) -> None:
            percent = 10 + (20 * current / total if total else 0)
            transfer.update_job({
                "stage": "download",
                "progress": percent,
                "message": "Скачиваю задания квестов…",
                "transferred_bytes": current,
                "total_bytes": total,
            })

        def scan_progress(percent: float, message: str) -> None:
            transfer.update_job({
                "stage": "client_scan",
                "progress": 30 + max(0.0, min(100.0, percent)) * 0.69,
                "message": message,
            })

        def work() -> dict[str, Any]:
            return self.panel.direct_status.export_translation_archive(
                self._credentials,
                str(item["id"]),
                client_directory,
                destination,
                progress=download_progress,
                scan_progress=scan_progress,
                cancelled=transfer.cancelled,
                paused=transfer.paused,
            )

        def success(result: dict[str, Any]) -> None:
            self.busy = False
            transfer.update_job({"stage": "complete", "progress": 100, "message": "Архив готов"})
            transfer.finish()
            tasks = int(result.get("tasks") or 0)
            mods = int(result.get("mods_incomplete") or 0)
            quests = int(result.get("quest_files") or 0)
            reviews = int(result.get("review_required") or 0)
            client = mapping(result.get("client"))
            packs = client.get("enabled_resourcepacks") if isinstance(client.get("enabled_resourcepacks"), list) else []
            message = (
                f"Найдено строк: {tasks}\n"
                f"Модов с неполным итоговым переводом: {mods}\n"
                f"Файлов серверных квестов: {quests}\n"
                f"Сомнительных строк для ручной проверки: {reviews}\n"
                f"Учтено активных ресурспаков: {len(packs)}\n\n"
                f"Архив сохранён:\n{destination}"
            )
            if client.get("resourcepack_mode") == "all_installed_options_missing":
                message += "\n\nФайл options.txt не найден: учтены все установленные ресурспаки."
            if result.get("task_limit_reached"):
                message += "\n\nДостигнут предел 250 000 строк; это отмечено в отчёте."
            self.status_var.set(f"Архив перевода готов · строк: {tasks}")
            self.panel.status("Материалы для перевода сохранены", seconds=15)
            messagebox.showinfo("Проверка перевода завершена", message, parent=self)

        def failure(error: Exception) -> None:
            self.busy = False
            transfer.finish()
            self.status_var.set("Проверка перевода завершилась ошибкой")
            messagebox.showerror("Проверка перевода", str(error), parent=self)

        self.panel.run_async(work, success, failure)

    def delete(self) -> None:
        item = self.selected()
        if not item:
            return
        if item["id"] == self.active_id:
            messagebox.showinfo("Удаление", "Сначала запустите другую сборку.", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("Удалить сборку")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Введите ID «{item['id']}» для подтверждения:", wraplength=440).pack(anchor="w")
        confirmation = tk.StringVar()
        enable_clipboard_paste(ttk.Entry(frame, textvariable=confirmation, width=42)).pack(fill="x", pady=8)
        delete_files = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(frame, text="Удалить также все файлы этой сборки", variable=delete_files)
        check.pack(anchor="w")
        if not item.get("managed_directory"):
            check.configure(state="disabled")
            ttk.Label(frame, text="Импортированная вручную папка останется на сервере.", style="Subtle.TLabel").pack(anchor="w", pady=4)

        def submit() -> None:
            if confirmation.get() != item["id"]:
                messagebox.showerror("Удаление", "ID не совпадает.", parent=dialog)
                return
            dialog.destroy()
            self._run_operation(
                {"action": "delete", "id": item["id"], "delete_files": delete_files.get()},
                "Удаляю сборку…",
                "Сборка удалена",
                timeout=3600,
            )

        ttk.Button(frame, text="Удалить", style="Danger.TButton", command=submit).pack(anchor="e", pady=(14, 0))
