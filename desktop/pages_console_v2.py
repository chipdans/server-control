"""Two direct SSH consoles: Debian administrator shell and Minecraft tmux."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from pages_base import BasePage
from ssh_terminal import EmbeddedTerminal


class TerminalTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, panel: Any, *, kind: str, title: str, help_text: str) -> None:
        super().__init__(parent, padding=(0, 8, 0, 0))
        self.panel = panel
        self.kind = kind
        self.loading = False

        actions = ttk.Frame(self, style="Card.TFrame", padding=(14, 11))
        actions.pack(fill="x", pady=(0, 10))
        ttk.Label(actions, text=help_text, style="SurfaceSubtle.TLabel", wraplength=820).pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="Отключить", command=self.disconnect).pack(side="right")
        ttk.Button(actions, text="↻  Подключить заново", style="Accent.TButton", command=self.connect).pack(side="right", padx=8)
        self.terminal = EmbeddedTerminal(self, title=title)
        self.terminal.pack(fill="both", expand=True)

    def connect(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.terminal.status_var.set("Проверяю право доступа…")

        def success(credentials: dict[str, Any]) -> None:
            self.loading = False
            self.terminal.connect(credentials)

        def failure(error: Exception) -> None:
            self.loading = False
            self.terminal.status_var.set(str(error))
            self.panel.status(str(error), error=True)

        self.panel.run_async(
            lambda: self.panel.api.terminal_credentials(self.kind),
            success,
            failure,
        )

    def disconnect(self) -> None:
        self.terminal.disconnect()
        self.terminal.status_var.set("Отключено")

    def close(self) -> None:
        self.terminal.close()


class ConsolePage(BasePage):
    page_id = "console"
    title = "Консоли"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.started = False
        self.tabs: dict[str, TerminalTab] = {}
        self.tab_ids: dict[str, str] = {}

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)
        if panel.state.has_permission("terminal.linux"):
            self._add_tab(
                "linux",
                "Linux",
                "Администраторская консоль Debian",
                "Полноценный интерактивный shell. После подключения открывается root через sudo -i.",
            )
        if panel.state.has_permission("terminal.minecraft"):
            self._add_tab(
                "minecraft",
                "Minecraft",
                "Прямая консоль Minecraft",
                "Прямое подключение к tmux-сессии Dragonfyre. RCON и промежуточная очередь команд не используются.",
            )
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)

    def _add_tab(self, kind: str, label: str, title: str, help_text: str) -> None:
        tab = TerminalTab(self.notebook, self.panel, kind=kind, title=title, help_text=help_text)
        self.notebook.add(tab, text=label)
        self.tabs[kind] = tab
        self.tab_ids[str(tab)] = kind

    def on_show(self) -> None:
        if not self.started:
            self.started = True
            self.after(100, self._connect_selected)

    def _tab_changed(self, _event: tk.Event | None = None) -> None:
        self._connect_selected()

    def _connect_selected(self) -> None:
        selected = self.notebook.select()
        kind = self.tab_ids.get(selected)
        tab = self.tabs.get(kind or "")
        if tab and not tab.terminal.connected and not tab.loading:
            tab.connect()

    def close(self) -> None:
        for tab in self.tabs.values():
            tab.close()
