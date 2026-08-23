"""Shared page contract for the Server Control sidebar."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from control_panel import ControlPanel


class BasePage(ttk.Frame):
    page_id = "base"
    title = ""

    def __init__(self, parent: tk.Misc, panel: "ControlPanel") -> None:
        super().__init__(parent, padding=16)
        self.panel = panel

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        return

    def on_show(self) -> None:
        self.update_state()

    def selected_instance(self) -> dict[str, Any] | None:
        return self.panel.state.selected_instance()

    def selected_instance_id(self) -> str | None:
        return self.panel.state.selected_instance_id


class EmptyState(ttk.Frame):
    def __init__(self, parent: tk.Misc, title: str, detail: str, action: tuple[str, Any] | None = None) -> None:
        super().__init__(parent, padding=30)
        ttk.Label(self, text=title, font=("Segoe UI", 14, "bold")).pack()
        ttk.Label(self, text=detail, style="Subtle.TLabel", wraplength=560, justify="center").pack(pady=(8, 14))
        if action:
            ttk.Button(self, text=action[0], command=action[1]).pack()

