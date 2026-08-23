"""Safe remote file browser/editor and backup manager."""

from __future__ import annotations

import datetime as dt
import os
import struct
import tkinter as tk
import urllib.parse
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from pages_base import BasePage
from widgets import TextEditor, TransferProgress, display_bytes, enable_clipboard_paste


def _stamp(value: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


class FilesPage(BasePage):
    page_id = "files"
    title = "Файлы"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.writable = panel.state.has_permission("minecraft.files.write")
        self.path = ""
        self.page = 1
        self.pages = 1
        self.loading = False
        self.loaded_instance: str | None = None
        self.entries: dict[str, dict[str, Any]] = {}
        self.back_stack: list[str] = []
        self.forward_stack: list[str] = []
        self.path_var = tk.StringVar(value="/")
        self.search_var = tk.StringVar()
        self.page_var = tk.StringVar(value="Страница 1 / 1")

        pathbar = ttk.Frame(self)
        pathbar.pack(fill="x", pady=(0, 6))
        ttk.Button(pathbar, text="⌂", width=3, command=lambda: self.open_directory("", 1)).pack(side="left")
        ttk.Button(pathbar, text="←", width=3, command=self.go_back).pack(side="left", padx=(4, 0))
        ttk.Button(pathbar, text="→", width=3, command=self.go_forward).pack(side="left", padx=4)
        ttk.Button(pathbar, text="↑", width=3, command=self.go_parent).pack(side="left", padx=4)
        path_entry = enable_clipboard_paste(ttk.Entry(pathbar, textvariable=self.path_var))
        path_entry.pack(side="left", fill="x", expand=True)
        path_entry.bind("<Return>", lambda _event: self.open_directory(self.path_var.get().strip("/"), 1))
        ttk.Button(pathbar, text="Перейти", command=lambda: self.open_directory(self.path_var.get().strip("/"), 1)).pack(side="left", padx=(5, 0))
        enable_clipboard_paste(ttk.Entry(pathbar, textvariable=self.search_var, width=24)).pack(side="left", padx=(12, 4))
        ttk.Button(pathbar, text="Поиск", command=self.search).pack(side="left")
        self.sort_var = tk.StringVar(value="name")
        sort = ttk.Combobox(pathbar, textvariable=self.sort_var, values=("name", "size", "modified", "type"), state="readonly", width=9)
        sort.pack(side="left", padx=(6, 0))
        sort.bind("<<ComboboxSelected>>", lambda _event: self.open_directory(self.path, 1, record_history=False))

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        actions: list[tuple[str, Any]] = [("Скачать", self.download)]
        if self.writable:
            actions = [
                ("Новая папка", lambda: self._named_operation("create_folder")),
                ("Новый файл", lambda: self._named_operation("create_file")),
                ("Загрузить", self.upload), *actions,
                ("Переименовать", lambda: self._named_operation("rename")),
                ("Копировать", lambda: self._destination_operation("copy")),
                ("Переместить", lambda: self._destination_operation("move")),
                ("Копия", lambda: self._named_operation("duplicate")),
                ("Архив", lambda: self.operation("archive")),
                ("Распаковать", lambda: self.operation("extract_zip")),
                ("Удалить", lambda: self.operation("delete")),
            ]
        for label, callback in actions:
            ttk.Button(toolbar, text=label, command=callback).pack(side="left", padx=(0, 4))
        ttk.Button(toolbar, text="server.properties", command=self.properties_dialog).pack(side="right")
        if self.writable:
            ttk.Button(toolbar, text="Иконка 64×64", command=self.upload_icon).pack(side="right", padx=5)
        ttk.Button(toolbar, text="★", width=3, command=self.toggle_favourite).pack(side="right")

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        browser = ttk.Frame(paned)
        editor_frame = ttk.Frame(paned)
        paned.add(browser, weight=3)
        paned.add(editor_frame, weight=4)
        self.tree = ttk.Treeview(browser, columns=("type", "size", "modified", "mode"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Имя")
        self.tree.column("#0", width=300, stretch=True)
        for column, title, width in (("type", "Тип", 90), ("size", "Размер", 100), ("modified", "Изменён", 135), ("mode", "Права", 80)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=False)
        scrollbar = ttk.Scrollbar(browser, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-Button-1>", self.open_selected)
        self.tree.bind("<Button-3>", self._context_menu)
        self.editor = TextEditor(editor_frame, save=self.save_editor, writable=self.writable)
        self.editor.pack(fill="both", expand=True)
        pager = ttk.Frame(self)
        pager.pack(fill="x", pady=(6, 0))
        ttk.Button(pager, text="←", command=lambda: self.open_directory(self.path, max(1, self.page - 1))).pack(side="left")
        ttk.Label(pager, textvariable=self.page_var).pack(side="left", padx=8)
        ttk.Button(pager, text="→", command=lambda: self.open_directory(self.path, min(self.pages, self.page + 1))).pack(side="left")
        ttk.Label(pager, text="Двойной клик открывает папку или текстовый файл. Символические ссылки недоступны.", style="Subtle.TLabel").pack(side="right")

    def on_show(self) -> None:
        instance_id = self.panel.selected_instance_id()
        if instance_id and instance_id != self.loaded_instance:
            if not self.editor.confirm_discard():
                return
            self.loaded_instance = instance_id
            self.path = ""
            self.open_directory("", 1)

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        if self.winfo_ismapped() and self.panel.selected_instance_id() != self.loaded_instance:
            self.on_show()

    def prepare_instance_change(self) -> bool:
        if not self.editor.confirm_discard():
            return False
        self.editor.clear_file()
        self.loaded_instance = None
        return True

    def open_directory(self, path: str, page: int = 1, *, record_history: bool = True) -> None:
        instance_id = self.panel.selected_instance_id()
        if not instance_id or self.loading or not self.panel.agent_available():
            return
        self.loading = True
        payload = {"instance_id": instance_id, "path": path.strip("/"), "page": page, "page_size": 200, "sort_by": self.sort_var.get()}

        def success(result: dict[str, Any]) -> None:
            self.loading = False
            opened_path = str(result.get("path", ""))
            if record_history and opened_path != self.path:
                self.back_stack.append(self.path)
                self.back_stack = self.back_stack[-100:]
                self.forward_stack.clear()
            self.path = opened_path
            self.page = int(result.get("page", 1))
            self.pages = int(result.get("pages", 1))
            self.path_var.set(f"/{self.path}" if self.path else "/")
            self.page_var.set(f"Страница {self.page} / {self.pages} · элементов {result.get('total', 0)}")
            self.entries.clear()
            self.tree.delete(*self.tree.get_children())
            for index, item in enumerate(result.get("entries", [])):
                if not isinstance(item, dict):
                    continue
                iid = str(index)
                self.entries[iid] = item
                marker = "📁 " if item.get("type") == "directory" else "🔗 " if item.get("is_symlink") else "📄 "
                self.tree.insert("", "end", iid=iid, text=f"{marker}{item.get('name')}", values=(item.get("type"), "—" if item.get("type") == "directory" else display_bytes(item.get("size")), _stamp(item.get("modified_at")), item.get("mode")))

        def failure(error: Exception) -> None:
            self.loading = False
            self.panel.handle_error(error, context="Открытие каталога")

        self.panel.run_async(lambda: self.panel.api.run_job("/v1/files/list", payload, timeout_seconds=60), success, failure, context="Файлы")

    def selected(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.entries.get(selection[0])

    def open_selected(self, _event: tk.Event | None = None) -> None:
        item = self.selected()
        if not item or item.get("is_symlink"):
            return
        if item.get("type") == "directory":
            if self.editor.confirm_discard():
                self.open_directory(str(item.get("path", "")), 1)
            return
        instance_id = self.panel.selected_instance_id()
        path = str(item.get("path", ""))
        if not instance_id or not self.panel.agent_available():
            return
        if self.editor.path != path and not self.editor.confirm_discard():
            return

        def success(result: dict[str, Any]) -> None:
            self.editor.load(path=path, content=str(result.get("content", "")), encoding=str(result.get("encoding", "utf-8")), mtime_ns=result.get("mtime_ns"))
            self.panel.preferences.remember("recent_files", f"{instance_id}:{path}")

        self.panel.run_async(lambda: self.panel.api.run_job("/v1/files/read", {"instance_id": instance_id, "path": path}, timeout_seconds=45), success, context="Открытие файла")

    def open_path(self, path: str) -> None:
        instance_id = self.panel.selected_instance_id()
        if not instance_id or not path or not self.panel.agent_available():
            return
        if self.editor.path != path and not self.editor.confirm_discard():
            return

        def success(result: dict[str, Any]) -> None:
            parent = str(PurePosixPath(path).parent)
            self.editor.load(path=path, content=str(result.get("content", "")), encoding=str(result.get("encoding", "utf-8")), mtime_ns=result.get("mtime_ns"))
            self.path = "" if parent == "." else parent
            self.path_var.set(f"/{self.path}" if self.path else "/")
            self.panel.preferences.remember("recent_files", f"{instance_id}:{path}")

        self.panel.run_async(lambda: self.panel.api.run_job("/v1/files/read", {"instance_id": instance_id, "path": path}, timeout_seconds=45), success, context="Открытие файла")

    def toggle_favourite(self) -> None:
        item = self.selected()
        instance_id = self.panel.selected_instance_id()
        path = str(item.get("path", "")) if item else self.editor.path
        if not instance_id or not path:
            self.panel.toast("Выберите файл для избранного", error=True)
            return
        value = f"{instance_id}:{path}"
        favourites = list(self.panel.preferences.get("favourite_files", []))
        if value in favourites:
            favourites.remove(value)
            self.panel.status("Файл удалён из избранного")
        else:
            favourites.insert(0, value)
            favourites = favourites[:50]
            self.panel.status("Файл добавлен в избранное")
        self.panel.preferences.set("favourite_files", favourites)

    def _context_menu(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Открыть", command=self.open_selected)
        menu.add_command(label="★ Избранное", command=self.toggle_favourite)
        menu.add_separator()
        menu.add_command(label="Скачать", command=self.download)
        if self.writable:
            menu.add_command(label="Переименовать", command=lambda: self._named_operation("rename"))
            menu.add_command(label="Дублировать", command=lambda: self._named_operation("duplicate"))
            menu.add_command(label="Удалить", command=lambda: self.operation("delete"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def upload_icon(self) -> None:
        if not self.writable:
            return
        if not self.panel.agent_available():
            return
        instance_id = self.panel.selected_instance_id()
        filename = filedialog.askopenfilename(parent=self, title="server-icon.png", filetypes=(("PNG", "*.png"),))
        if not instance_id or not filename:
            return
        path = Path(filename)
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Иконка превышает 2 МиБ")
            with path.open("rb") as source:
                header = source.read(24)
            if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                raise ValueError("Файл не является PNG")
            width, height = struct.unpack(">II", header[16:24])
            if (width, height) != (64, 64):
                raise ValueError(f"Нужен PNG 64×64, выбран {width}×{height}")
        except (OSError, ValueError, struct.error) as error:
            messagebox.showerror("Иконка сервера", str(error), parent=self)
            return
        transfer = TransferProgress(self, "Загрузка server-icon.png")

        def success(_result: dict[str, Any]) -> None:
            transfer.finish()
            self.panel.status("server-icon.png обновлён; Minecraft применит его после restart")
            self.open_directory(self.path, self.page)

        def failure(error: Exception) -> None:
            transfer.finish()
            self.panel.handle_error(error, context="Иконка сервера")

        self.panel.run_async(
            lambda: self.panel.api.upload_file(path, instance_id, "", remote_name="server-icon.png", overwrite=True, progress=transfer.update_job, cancelled=transfer.cancelled, paused=transfer.paused),
            success, failure, context="Иконка сервера",
        )

    def save_editor(self, content: str) -> None:
        if not self.writable:
            self.panel.toast("Нет права изменения файлов", error=True)
            return
        instance_id = self.panel.selected_instance_id()
        if not instance_id or not self.editor.path:
            return
        payload = {"instance_id": instance_id, "path": self.editor.path, "content": content, "encoding": self.editor.encoding, "expected_mtime_ns": self.editor.mtime_ns}

        def success(result: dict[str, Any]) -> None:
            self.editor.mark_saved(result.get("mtime_ns"))
            backup = result.get("safety_backup")
            self.panel.status("Файл сохранён" + (f" · safety backup: {backup}" if backup else ""))
            self.open_directory(self.path, self.page)

        self.panel.run_async(lambda: self.panel.api.run_job("/v1/files/write", payload, timeout_seconds=60), success, context="Сохранение файла")

    def go_parent(self) -> None:
        parent = str(PurePosixPath(self.path).parent) if self.path else ""
        self.open_directory("" if parent == "." else parent, 1)

    def go_back(self) -> None:
        if not self.back_stack:
            return
        target = self.back_stack.pop()
        self.forward_stack.append(self.path)
        self.open_directory(target, 1, record_history=False)

    def go_forward(self) -> None:
        if not self.forward_stack:
            return
        target = self.forward_stack.pop()
        self.back_stack.append(self.path)
        self.open_directory(target, 1, record_history=False)

    def _named_operation(self, action: str) -> None:
        if not self.writable:
            return
        selected = self.selected()
        if action in {"create_folder", "create_file"}:
            source = self.path
            prompt = "Имя новой папки" if action == "create_folder" else "Имя нового файла"
        elif not selected:
            messagebox.showinfo("Файлы", "Выберите файл или папку.")
            return
        else:
            source = str(selected.get("path", ""))
            prompt = "Новое имя" if action == "rename" else "Имя копии"
        name = simpledialog.askstring("Файлы", prompt, parent=self, initialvalue=str(selected.get("name", "")) if selected and action == "rename" else "")
        if not name:
            return
        self.operation(action, path=source, name=name)

    def operation(self, action: str, *, path: str | None = None, name: str = "") -> None:
        if not self.writable:
            self.panel.toast("Нет права изменения файлов", error=True)
            return
        item = self.selected()
        source = path if path is not None else str(item.get("path", "")) if item else ""
        if not source and action not in {"create_folder", "create_file"}:
            messagebox.showinfo("Файлы", "Выберите файл или папку.")
            return
        if action == "delete" and not self.panel.confirm("Удаление", f"Удалить {source}? Операцию нельзя отменить.", dangerous=True):
            return
        payload = {"instance_id": self.panel.selected_instance_id(), "action": action, "path": source, "name": name}
        self.panel.run_job("/v1/files/operation", payload, context=f"Файлы: {action}", timeout=24 * 60 * 60, success=lambda _result: self.open_directory(self.path, self.page))

    def _destination_operation(self, action: str) -> None:
        if not self.writable:
            return
        item = self.selected()
        if not item:
            messagebox.showinfo("Файлы", "Выберите файл или папку.")
            return
        destination = simpledialog.askstring("Файлы", "Каталог назначения относительно корня сборки", parent=self, initialvalue=self.path)
        if destination is None:
            return
        payload = {"instance_id": self.panel.selected_instance_id(), "action": action, "path": item.get("path"), "destination": destination.strip("/"), "name": ""}
        self.panel.run_job("/v1/files/operation", payload, context=f"Файлы: {action}", timeout=24 * 60 * 60, success=lambda _result: self.open_directory(self.path, self.page))

    def upload(self) -> None:
        if not self.writable:
            return
        if not self.panel.agent_available():
            return
        instance_id = self.panel.selected_instance_id()
        filenames = filedialog.askopenfilenames(parent=self)
        if not instance_id or not filenames:
            return
        sources = [Path(filename) for filename in filenames]
        destination = self.path
        visible_names = {str(item.get("name")) for item in self.entries.values()}
        overwrite = any(source.name in visible_names for source in sources)
        if overwrite and not self.panel.confirm("Замена файлов", "Один или несколько файлов уже существуют в текущей папке. Атомарно заменить их после проверки SHA-256?", dangerous=True):
            return
        transfer = TransferProgress(self, f"Загрузка файлов: {len(sources)}")

        def success(_result: dict[str, Any]) -> None:
            transfer.finish()
            self.panel.status("Файл загружен")
            self.open_directory(self.path, self.page)

        def failure(error: Exception) -> None:
            transfer.finish()
            self.panel.handle_error(error, context="Загрузка файла")

        self.panel.run_async(
            lambda: self._upload_many(sources, instance_id, destination, transfer, overwrite),
            success, failure, context="Загрузка файла",
        )

    def _upload_many(self, sources: list[Path], instance_id: str, destination: str, transfer: TransferProgress, overwrite: bool) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        total_files = max(1, len(sources))
        for index, source in enumerate(sources):
            def progress(job: dict[str, Any], position: int = index, name: str = source.name) -> None:
                value = float(job.get("progress", 0) or 0)
                transfer.update_job({**job, "progress": (position * 100 + value) / total_files, "message": f"{name}: {job.get('message', '')}"})

            results.append(self.panel.api.upload_file(source, instance_id, destination, overwrite=overwrite, progress=progress, cancelled=transfer.cancelled, paused=transfer.paused))
        return {"files": results}

    def download(self) -> None:
        item = self.selected()
        instance_id = self.panel.selected_instance_id()
        if not item or not instance_id:
            messagebox.showinfo("Скачивание", "Выберите файл или папку.")
            return
        if not self.panel.agent_available():
            return
        default = str(item.get("name") or "download") + (".zip" if item.get("type") == "directory" else "")
        filename = filedialog.asksaveasfilename(parent=self, initialfile=default)
        if not filename:
            return
        transfer = TransferProgress(self, f"Скачивание {item.get('name')}")

        def success(result: dict[str, Any]) -> None:
            transfer.finish()
            self.panel.status(f"Сохранено: {result.get('path')}")

        def failure(error: Exception) -> None:
            transfer.finish()
            self.panel.handle_error(error, context="Скачивание")

        self.panel.run_async(
            lambda: self.panel.api.download_file(instance_id, str(item.get("path")), Path(filename), progress=transfer.update_job, cancelled=transfer.cancelled, paused=transfer.paused),
            success, failure, context="Скачивание",
        )

    def search(self) -> None:
        query = self.search_var.get().strip()
        instance_id = self.panel.selected_instance_id()
        if not query or not instance_id or not self.panel.agent_available():
            return

        def success(result: dict[str, Any]) -> None:
            self.entries.clear()
            self.tree.delete(*self.tree.get_children())
            for index, item in enumerate(result.get("results", [])):
                iid = str(index)
                self.entries[iid] = item
                self.tree.insert("", "end", iid=iid, text=str(item.get("path")), values=(item.get("type"), display_bytes(item.get("size")), _stamp(item.get("modified_at")), "content" if item.get("content_match") else "name"))
            self.page_var.set(f"Результатов: {len(self.entries)}" + (" · список ограничен" if result.get("truncated") else ""))

        self.panel.run_async(lambda: self.panel.api.run_job("/v1/files/search", {"instance_id": instance_id, "path": self.path, "query": query, "pattern": "*", "include_content": True}, timeout_seconds=300), success, context="Поиск файлов")

    def properties_dialog(self) -> None:
        instance_id = self.panel.selected_instance_id()
        if not instance_id or not self.panel.agent_available():
            return

        def loaded(result: dict[str, Any]) -> None:
            content = str(result.get("content", ""))
            values: dict[str, str] = {}
            for line in content.splitlines():
                key, separator, value = line.partition("=")
                if separator and key and not key.lstrip().startswith("#"):
                    values[key.strip()] = value.strip()
            dialog = tk.Toplevel(self)
            dialog.title("server.properties")
            dialog.transient(self.winfo_toplevel())
            dialog.grab_set()
            frame = ttk.Frame(dialog, padding=18)
            frame.pack(fill="both", expand=True)
            fields: dict[str, tk.StringVar] = {}
            definitions = (("motd", "MOTD"), ("server-port", "Порт"), ("max-players", "Максимум игроков"), ("difficulty", "Сложность"), ("gamemode", "Режим игры"), ("view-distance", "Дальность прорисовки"), ("simulation-distance", "Дальность симуляции"), ("allow-flight", "Полёт: true/false"), ("white-list", "Whitelist: true/false"), ("pvp", "PvP: true/false"), ("enable-command-block", "Command blocks: true/false"), ("online-mode", "Online mode: true/false"))
            for row, (key, label) in enumerate(definitions):
                fields[key] = tk.StringVar(value=values.get(key, ""))
                ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
                enable_clipboard_paste(ttk.Entry(frame, textvariable=fields[key], width=48)).grid(row=row, column=1, sticky="ew", pady=4)

            def save() -> None:
                updated = content
                for key, variable in fields.items():
                    value = variable.get().replace("\r", " ").replace("\n", " ")
                    pattern = f"{key}="
                    lines = updated.splitlines()
                    replaced = False
                    for index, line in enumerate(lines):
                        if line.startswith(pattern):
                            lines[index] = f"{key}={value}"
                            replaced = True
                            break
                    if not replaced:
                        lines.append(f"{key}={value}")
                    updated = "\n".join(lines) + ("\n" if content.endswith("\n") else "")
                dialog.destroy()
                payload = {"instance_id": instance_id, "path": "server.properties", "content": updated, "encoding": result.get("encoding", "utf-8"), "expected_mtime_ns": result.get("mtime_ns")}
                self.panel.run_job("/v1/files/write", payload, context="server.properties")

            if self.writable:
                ttk.Button(frame, text="Сохранить", command=save).grid(row=len(definitions), column=1, sticky="e", pady=(12, 0))
            else:
                ttk.Label(frame, text="Режим только чтения", style="Subtle.TLabel").grid(row=len(definitions), column=1, sticky="e", pady=(12, 0))
            frame.columnconfigure(1, weight=1)

        self.panel.run_async(lambda: self.panel.api.run_job("/v1/files/read", {"instance_id": instance_id, "path": "server.properties"}, timeout_seconds=45), loaded, context="server.properties")


class BackupsPage(BasePage):
    page_id = "backups"
    title = "Резервные копии"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="Создать backup", command=self.create).pack(side="left")
        if panel.state.has_permission("minecraft.settings"):
            ttk.Button(toolbar, text="Настройки хранения", command=self.settings).pack(side="left", padx=(6, 0))
        if panel.state.has_permission("minecraft.restore"):
            ttk.Button(toolbar, text="Восстановить", command=self.restore).pack(side="left", padx=6)
            ttk.Button(toolbar, text="Новая сборка из backup", command=self.duplicate).pack(side="left")
        ttk.Button(toolbar, text="Скачать", command=self.download).pack(side="right")
        ttk.Button(toolbar, text="Удалить", style="Danger.TButton", command=self.delete).pack(side="right", padx=6)
        self.tree = ttk.Treeview(self, columns=("instance", "created", "size", "files", "reason", "comment"), show="headings")
        for column, label, width in (("instance", "Сборка", 120), ("created", "Создан", 145), ("size", "Размер", 100), ("files", "Файлов", 80), ("reason", "Причина", 100), ("comment", "Комментарий", 400)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, stretch=column == "comment")
        self.tree.pack(fill="both", expand=True)
        self.cache: dict[str, dict[str, Any]] = {}
        ttk.Label(self, text="Восстановление автоматически останавливает Minecraft, создаёт safety backup и атомарно заменяет каталог.", style="Subtle.TLabel").pack(anchor="w", pady=(8, 0))

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        status = self.panel.state.server.get("status") if isinstance(self.panel.state.server.get("status"), dict) else {}
        backups = status.get("backups") if isinstance(status.get("backups"), list) else []
        selected = self.panel.selected_instance_id()
        self.cache.clear()
        self.tree.delete(*self.tree.get_children())
        for item in backups:
            if not isinstance(item, dict) or (selected and item.get("instance_id") != selected):
                continue
            identifier = str(item.get("id"))
            self.cache[identifier] = item
            self.tree.insert("", "end", iid=identifier, values=(item.get("instance_id"), _stamp(item.get("created_at")), display_bytes(item.get("size")), item.get("files"), item.get("reason"), item.get("comment")))

    def selected_backup(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Backups", "Выберите резервную копию.")
            return None
        return self.cache.get(selection[0])

    def create(self) -> None:
        comment = simpledialog.askstring("Backup", "Комментарий (необязательно)", parent=self) or ""
        instance_id = self.panel.selected_instance_id()
        if instance_id:
            self.panel.run_job("/v1/backups/action", {"action": "create", "instance_id": instance_id, "reason": "manual", "comment": comment}, context="Создание backup", timeout=24 * 60 * 60)

    def settings(self) -> None:
        instance = self.panel.state.selected_instance()
        if not instance:
            return
        current = instance.get("backup") if isinstance(instance.get("backup"), dict) else {}
        dialog = tk.Toplevel(self)
        dialog.title(f"Backup · {instance.get('name')}")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        keep = tk.StringVar(value=str(current.get("keep_last", 10)))
        maximum_gb = tk.StringVar(value=str(round(int(current.get("max_total_bytes", 0) or 0) / 1024**3, 2)))
        before_start = tk.BooleanVar(value=bool(current.get("before_start")))
        for row, (label, variable) in enumerate((("Хранить последние N", keep), ("Максимальный общий размер, ГиБ (0 — без лимита)", maximum_gb))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=variable, width=18)).grid(row=row, column=1, sticky="w", padx=8)
        ttk.Checkbutton(frame, text="Создавать backup перед каждым запуском", variable=before_start).grid(row=2, column=0, columnspan=2, sticky="w", pady=7)
        ttk.Label(frame, text="Расписание для всех сборок настраивается на странице «Настройки → Автоматизация». Перед restore и обновлением ZIP safety backup создаётся всегда.", style="Subtle.TLabel", wraplength=520).grid(row=3, column=0, columnspan=2, sticky="w", pady=8)

        def save() -> None:
            try:
                keep_last = int(keep.get())
                max_bytes = round(float(maximum_gb.get().replace(",", ".")) * 1024**3)
            except ValueError:
                messagebox.showerror("Backup", "Проверьте числовые значения.", parent=dialog)
                return
            if not 1 <= keep_last <= 500 or max_bytes < 0:
                messagebox.showerror("Backup", "Хранить можно от 1 до 500 копий; размер не может быть отрицательным.", parent=dialog)
                return
            payload = {"backup": {**current, "keep_last": keep_last, "max_total_bytes": max_bytes, "before_start": before_start.get()}}
            dialog.destroy()
            self.panel.run_job(f"/v1/instances/{urllib.parse.quote(str(instance['id']), safe='')}", payload, method="PATCH", context="Настройки backup")

        ttk.Button(frame, text="Сохранить", command=save).grid(row=4, column=1, sticky="e", pady=(10, 0))

    def restore(self) -> None:
        if not self.panel.state.has_permission("minecraft.restore"):
            return
        item = self.selected_backup()
        if not item or not self.panel.confirm("Восстановление", "Minecraft будет остановлен. Перед заменой файлов будет создан safety backup. Продолжить?", dangerous=True):
            return
        self.panel.run_job("/v1/backups/action", {"action": "restore", "instance_id": item.get("instance_id"), "backup_id": item.get("id")}, context="Восстановление backup", timeout=24 * 60 * 60)

    def delete(self) -> None:
        item = self.selected_backup()
        if not item or not self.panel.confirm("Удаление backup", f"Удалить {item.get('id')}?", dangerous=True):
            return
        self.panel.run_job("/v1/backups/action", {"action": "delete", "instance_id": item.get("instance_id"), "backup_id": item.get("id")}, context="Удаление backup")

    def duplicate(self) -> None:
        if not self.panel.state.has_permission("minecraft.restore"):
            return
        item = self.selected_backup()
        if not item:
            return
        new_id = simpledialog.askstring("Новая сборка", "ID новой сборки", parent=self)
        if not new_id:
            return
        name = simpledialog.askstring("Новая сборка", "Название", parent=self) or new_id
        self.panel.run_job("/v1/backups/action", {"action": "duplicate", "instance_id": item.get("instance_id"), "backup_id": item.get("id"), "new_instance_id": new_id, "name": name}, context="Сборка из backup", timeout=24 * 60 * 60)

    def download(self) -> None:
        item = self.selected_backup()
        if not item:
            return
        if not self.panel.agent_available():
            return
        filename = filedialog.asksaveasfilename(parent=self, initialfile=str(item.get("download_name") or f"{item.get('id')}.zip"), defaultextension=".zip")
        if not filename:
            return
        transfer = TransferProgress(self, f"Скачивание backup {item.get('id')}")

        def success(result: dict[str, Any]) -> None:
            transfer.finish()
            self.panel.status(f"Backup сохранён: {result.get('path')}")

        def failure(error: Exception) -> None:
            transfer.finish()
            self.panel.handle_error(error, context="Скачивание backup")

        self.panel.run_async(
            lambda: self.panel.api.download_backup(str(item.get("instance_id")), str(item.get("id")), Path(filename), progress=transfer.update_job, cancelled=transfer.cancelled, paused=transfer.paused),
            success, failure, context="Скачивание backup",
        )
