#!/usr/bin/env python3
"""Server Control desktop entry point.

Authentication and self-update stay deliberately small here. The connected
control panel and its pages live in separate modules so background work never
blocks Tk's main thread.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import queue
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from api import ApiClient
from control_panel import ControlPanel
from state import LocalPreferences
from updater import download_update, is_newer, latest_release, launch_updater
from widgets import (
    display_bytes,
    display_duration,
    display_percent,
    enable_clipboard_paste,
    is_legacy_agent_network_error,
    is_rcon_lifecycle_message,
    minecraft_completion_candidates,
    numeric_value,
    replace_minecraft_completion,
)


APP_VERSION = "1.0.2"
APP_TITLE = "Server Control"


class ConfigurationError(RuntimeError):
    pass


def application_directory() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def preferences_path() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or application_directory()) / "ServerControl"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "server-control"
    return root / "ui-preferences.json"


def load_configuration() -> dict[str, Any]:
    path = application_directory() / "server-control.json"
    if not path.is_file():
        raise ConfigurationError(
            "Не найден server-control.json рядом с программой. "
            "Скопируйте server-control.json.example и укажите HTTPS-адрес Worker."
        )
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Не удалось прочитать server-control.json: {error}") from error
    url = config.get("api_base_url")
    if not isinstance(url, str) or not url.startswith("https://") or "YOUR-SUBDOMAIN" in url:
        raise ConfigurationError("В server-control.json нужен реальный HTTPS-адрес Cloudflare Worker.")
    return config


class ServerControlApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_TITLE} {APP_VERSION}")
        self.root.minsize(1024, 680)
        self.root.option_add("*tearOff", False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.closed = False
        self.update_in_progress = False
        self.panel: ControlPanel | None = None
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.api: ApiClient | None = None
        self.user: dict[str, Any] | None = None
        self.preferences = LocalPreferences(preferences_path())
        geometry = str(self.preferences.get("window_geometry", "1280x820"))
        try:
            self.root.geometry(geometry)
        except tk.TclError:
            self.root.geometry("1280x820")
        self._configure_style(str(self.preferences.get("theme", "dark")))
        self.root.after(30, self._drain_ui_queue)
        try:
            self.config = load_configuration()
            self.api = ApiClient(str(self.config["api_base_url"]))
            self.show_login()
            self.root.after(1200, self._mark_update_healthy)
        except ConfigurationError as error:
            self.config = {}
            self.show_configuration_error(str(error))

    def _configure_style(self, theme: str) -> None:
        dark = theme != "light"
        colors = {
            "bg": "#10171d" if dark else "#f4f6f8",
            "panel": "#172129" if dark else "#ffffff",
            "sidebar": "#0b1116" if dark else "#e8edf2",
            "text": "#e6eef5" if dark else "#17212b",
            "muted": "#91a0ad" if dark else "#5f6b7a",
            "accent": "#3f8cff",
            "danger": "#ff6b6b" if dark else "#a61b1b",
            "warning": "#ffd166" if dark else "#8a5a00",
        }
        self.root.configure(background=colors["bg"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=colors["bg"], foreground=colors["text"], fieldbackground=colors["panel"], font=("Segoe UI", 10))
        style.configure("TFrame", background=colors["bg"])
        style.configure("TLabelframe", background=colors["bg"], foreground=colors["text"], bordercolor="#34424d")
        style.configure("TLabelframe.Label", background=colors["bg"], foreground=colors["text"], font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background=colors["bg"], foreground=colors["text"])
        style.configure("Subtle.TLabel", foreground=colors["muted"])
        style.configure("Warning.TLabel", foreground=colors["warning"])
        style.configure("Connection.TLabel", foreground="#63e6be")
        style.configure("Sidebar.TFrame", background=colors["sidebar"])
        style.configure("SidebarTitle.TLabel", background=colors["sidebar"], foreground=colors["text"], font=("Segoe UI", 15, "bold"))
        style.configure("SidebarSubtle.TLabel", background=colors["sidebar"], foreground=colors["muted"])
        style.configure("Nav.TButton", anchor="w", padding=(10, 7), background=colors["sidebar"], foreground=colors["text"], borderwidth=0)
        style.map("Nav.TButton", background=[("active", "#23313c")])
        style.configure("TButton", padding=(9, 5), background=colors["panel"], foreground=colors["text"])
        style.configure("Danger.TButton", foreground=colors["danger"])
        style.configure("TEntry", padding=5, fieldbackground=colors["panel"], foreground=colors["text"], insertcolor=colors["text"])
        style.configure("TCombobox", padding=4, fieldbackground=colors["panel"], foreground=colors["text"])
        style.configure("Treeview", background=colors["panel"], fieldbackground=colors["panel"], foreground=colors["text"], rowheight=25)
        style.configure("Treeview.Heading", background="#25323c" if dark else "#e1e7ec", foreground=colors["text"], padding=5)
        style.map("Treeview", background=[("selected", colors["accent"])], foreground=[("selected", "#ffffff")])
        style.configure("TNotebook", background=colors["bg"])
        style.configure("TNotebook.Tab", padding=(12, 6), background=colors["panel"], foreground=colors["text"])
        style.map("TNotebook.Tab", background=[("selected", colors["accent"])], foreground=[("selected", "#ffffff")])
        style.configure("Horizontal.TProgressbar", troughcolor=colors["panel"], background=colors["accent"])

    def clear(self) -> None:
        if self.panel:
            self.panel.close()
            self.panel = None
        for child in self.root.winfo_children():
            child.destroy()

    def show_configuration_error(self, text: str) -> None:
        self.clear()
        frame = ttk.Frame(self.root, padding=40)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Нужно настроить подключение", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text=text, wraplength=760).pack(anchor="w", pady=16)
        ttk.Label(frame, text=f"Ожидаемый файл: {application_directory() / 'server-control.json'}", style="Subtle.TLabel").pack(anchor="w")

    def show_login(self) -> None:
        self.clear()
        frame = ttk.Frame(self.root, padding=40)
        frame.place(relx=0.5, rely=0.46, anchor="center")
        ttk.Label(frame, text="Server Control", font=("Segoe UI", 24, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(frame, text=f"Безопасное управление Debian и Minecraft · {APP_VERSION}", style="Subtle.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 20))
        username = tk.StringVar()
        password = tk.StringVar()
        status = tk.StringVar()
        ttk.Label(frame, text="Логин").grid(row=2, column=0, sticky="w", pady=5)
        username_entry = enable_clipboard_paste(ttk.Entry(frame, textvariable=username, width=36))
        username_entry.grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Пароль").grid(row=3, column=0, sticky="w", pady=5)
        password_entry = enable_clipboard_paste(ttk.Entry(frame, textvariable=password, show="•", width=36))
        password_entry.grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Label(frame, textvariable=status, style="Warning.TLabel", wraplength=440).grid(row=4, column=0, columnspan=2, sticky="w", pady=5)
        button = ttk.Button(frame, text="Войти")
        button.grid(row=5, column=1, sticky="e", pady=(12, 0))
        ttk.Button(frame, text="Первоначальная настройка", command=self.show_setup).grid(row=5, column=0, sticky="w", pady=(12, 0))

        def submit(_event: tk.Event | None = None) -> None:
            if not username.get().strip() or not password.get():
                status.set("Введите логин и пароль.")
                return
            button.configure(state="disabled")
            status.set("Вход…")

            def success(result: dict[str, Any]) -> None:
                self.user = result.get("user") if isinstance(result.get("user"), dict) else {}
                self.show_panel()

            def failure(error: Exception) -> None:
                button.configure(state="normal")
                status.set(str(error))

            self.async_call(lambda: self.require_api().login(username.get().strip(), password.get()), success, failure)

        button.configure(command=submit)
        password_entry.bind("<Return>", submit)
        username_entry.focus_set()
        frame.columnconfigure(1, weight=1)

    def show_setup(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Первоначальная настройка владельца")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        variables = (tk.StringVar(), tk.StringVar(), tk.StringVar())
        for row, (label, variable, secret) in enumerate((("BOOTSTRAP_KEY", variables[0], True), ("Логин владельца", variables[1], False), ("Пароль (от 12 символов)", variables[2], True))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=variable, show="•" if secret else "", width=36)).grid(row=row, column=1, sticky="ew", pady=5)

        def submit() -> None:
            def success(result: dict[str, Any]) -> None:
                dialog.destroy()
                self.user = result.get("user") if isinstance(result.get("user"), dict) else {}
                self.show_panel()

            self.async_call(
                lambda: self.require_api().setup_owner(variables[0].get(), variables[1].get().strip(), variables[2].get()),
                success,
                lambda error: messagebox.showerror("Настройка", str(error), parent=dialog),
            )

        ttk.Button(frame, text="Создать владельца", command=submit).grid(row=3, column=1, sticky="e", pady=(12, 0))
        frame.columnconfigure(1, weight=1)

    def show_panel(self) -> None:
        if not self.user:
            self.show_login()
            return
        self.clear()
        self.panel = ControlPanel(
            self.root,
            api=self.require_api(),
            user=self.user,
            preferences=self.preferences,
            logout=self.logout,
            check_client_update=lambda: self.check_for_updates(manual=True),
            client_version=APP_VERSION,
        )
        self.panel.pack(fill="both", expand=True)
        self.root.after(1800, self.check_for_updates)

    def check_for_updates(self, manual: bool = False) -> None:
        if self.update_in_progress:
            return
        update = self.config.get("update") if isinstance(self.config.get("update"), dict) else {}
        if not update.get("enabled", True) or not getattr(sys, "frozen", False):
            if manual:
                messagebox.showinfo("Обновления", "Автообновление проверяется только в собранной Windows-версии.")
            return
        repository = str(update.get("repository", "chipdans/server-control"))
        asset = str(update.get("asset_name", "ServerControl-Update.zip"))

        def work() -> dict[str, Any] | None:
            release = latest_release(repository, asset)
            return release if release and is_newer(str(release.get("tag")), APP_VERSION) else None

        def success(release: dict[str, Any] | None) -> None:
            if not release:
                if manual:
                    messagebox.showinfo("Обновления", f"Установлена актуальная версия {APP_VERSION}.")
                return
            automatic = bool(update.get("install_automatically", True)) and not manual
            if not automatic and not messagebox.askyesno("Обновление", f"Доступна версия {release['tag']}. Установить сейчас?"):
                return
            self.update_in_progress = True
            if self.panel:
                self.panel.status(f"Скачиваю {release['tag']}…", seconds=60)

            def install() -> None:
                archive = download_update(str(release["url"]), expected_sha256=release.get("sha256"))
                launch_updater(archive, Path(sys.executable).resolve())

            def failure(error: Exception) -> None:
                self.update_in_progress = False
                messagebox.showerror("Обновление", str(error))

            self.async_call(install, lambda _value: self.close(), failure)

        self.async_call(work, success, lambda error: messagebox.showerror("Обновления", str(error)) if manual else None)

    def _mark_update_healthy(self) -> None:
        prefix = "--update-health-file="
        argument = next((value[len(prefix):] for value in sys.argv[1:] if value.startswith(prefix)), "")
        if not argument:
            return
        path = Path(argument)
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(APP_VERSION)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except OSError:
            if temporary:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def async_call(
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
                    self._ui_queue.put(lambda caught=error: failure(caught) if failure else messagebox.showerror("Server Control", str(caught)))
            else:
                if not self.closed:
                    self._ui_queue.put(lambda completed=result: success(completed))

        threading.Thread(target=runner, daemon=True).start()

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
        self.root.after(30, self._drain_ui_queue)

    def require_api(self) -> ApiClient:
        if not self.api:
            raise RuntimeError("API не настроен")
        return self.api

    def logout(self) -> None:
        if self.api:
            self.api.token = None
        self.user = None
        self.show_login()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.panel:
            self.panel.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ServerControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
