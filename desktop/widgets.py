"""Reusable modern Tk widgets for consoles, editors, cards and palettes."""

from __future__ import annotations

import datetime as dt
import math
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable


def numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 and math.isfinite(result) else None


def display_bytes(value: Any, *, per_second: bool = False) -> str:
    amount = numeric_value(value)
    if amount is None:
        return "—"
    suffix = "/s" if per_second else ""
    units = ("B", "Kb", "Mb", "Gb", "Tb")
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    precision = 0 if index == 0 or amount >= 100 else 1
    return f"{amount:.{precision}f} {units[index]}{suffix}"


def display_duration(value: Any) -> str:
    seconds = numeric_value(value)
    if seconds is None:
        return "—"
    total = int(seconds)
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {seconds} с"
    return f"{seconds} с"


def display_percent(value: Any) -> tuple[float, str]:
    number = numeric_value(value)
    if number is None:
        return 0.0, "—"
    bounded = max(0.0, min(100.0, number))
    return bounded, f"{bounded:.1f}%"


def enable_clipboard_paste(entry: ttk.Entry) -> ttk.Entry:
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

    def physical_key(event: tk.Event) -> str | None:
        if event.keycode == 86 or str(event.keysym).lower() in {"v", "cyrillic_em"}:
            return paste(event)
        return None

    entry.bind("<<Paste>>", paste)
    entry.bind("<Control-KeyPress>", physical_key, add="+")
    entry.bind("<Shift-Insert>", paste)
    return entry


DEFAULT_MINECRAFT_COMMANDS = (
    "advancement", "attribute", "ban", "ban-ip", "banlist", "bossbar", "clear", "clone", "damage", "data",
    "datapack", "debug", "defaultgamemode", "difficulty", "effect", "enchant", "execute", "experience", "fill",
    "fillbiome", "forceload", "function", "gamemode", "gamerule", "give", "help", "item", "jfr", "kick", "kill",
    "list", "locate", "loot", "me", "msg", "op", "pardon", "pardon-ip", "particle", "place", "playsound",
    "random", "recipe", "reload", "ride", "save-all", "save-off", "save-on", "say", "schedule", "scoreboard",
    "seed", "setblock", "setidletimeout", "setworldspawn", "spawnpoint", "spectate", "spreadplayers", "stop",
    "stopsound", "summon", "tag", "team", "teammsg", "teleport", "tell", "tellraw", "time", "title", "tm",
    "tp", "trigger", "weather", "whitelist", "worldborder", "xp",
)
MINECRAFT_SELECTORS = ("@a", "@e", "@p", "@r", "@s")
MINECRAFT_GAMERULES = (
    "announceAdvancements", "commandBlockOutput", "disableRaids", "doDaylightCycle", "doEntityDrops", "doFireTick",
    "doImmediateRespawn", "doInsomnia", "doMobLoot", "doMobSpawning", "doPatrolSpawning", "doTileDrops",
    "doTraderSpawning", "doWeatherCycle", "fallDamage", "fireDamage", "keepInventory", "mobGriefing",
    "naturalRegeneration", "playersSleepingPercentage", "randomTickSpeed", "sendCommandFeedback", "showDeathMessages",
)


def _unique_sorted(values: Any) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()}, key=str.casefold)


def minecraft_completion_candidates(value: str, command_names: list[str], players: list[str]) -> list[str]:
    text = value.lstrip()
    has_slash = text.startswith("/")
    raw = text[1:] if has_slash else text
    ends_with_space = raw.endswith(" ")
    tokens = raw.split()
    if not tokens or (len(tokens) == 1 and not ends_with_space):
        partial = tokens[0] if tokens else ""
        prefix = "/" if has_slash else ""
        return [f"{prefix}{name}" for name in _unique_sorted([*DEFAULT_MINECRAFT_COMMANDS, *command_names]) if name.casefold().startswith(partial.casefold())][:20]
    command = tokens[0].casefold()
    arguments = tokens[1:]
    index = len(arguments) if ends_with_space else len(arguments) - 1
    partial = "" if ends_with_space else arguments[-1]
    targets = _unique_sorted([*players, *MINECRAFT_SELECTORS])
    options: list[str] = []
    if command in {"ban", "clear", "deop", "effect", "enchant", "experience", "give", "kick", "kill", "msg", "op", "pardon", "recipe", "spawnpoint", "spectate", "tag", "teammsg", "teleport", "tell", "tp", "xp"}:
        options = targets
    if command in {"gamemode", "defaultgamemode"}:
        options = ["survival", "creative", "adventure", "spectator"] if index == 0 else targets
    elif command == "difficulty":
        options = ["peaceful", "easy", "normal", "hard"]
    elif command == "weather":
        options = ["clear", "rain", "thunder"]
    elif command == "time":
        options = ["set", "add", "query"] if index == 0 else ["day", "night", "noon", "midnight"]
    elif command == "whitelist":
        options = ["on", "off", "list", "add", "remove", "reload"] if index == 0 else targets
    elif command == "gamerule":
        options = list(MINECRAFT_GAMERULES) if index == 0 else ["true", "false"]
    elif command == "execute":
        options = ["as", "at", "positioned", "rotated", "facing", "align", "anchored", "in", "if", "unless", "store", "run"]
    elif command == "scoreboard":
        options = ["objectives", "players"] if index == 0 else ["add", "remove", "setdisplay", "list"]
    elif command == "team":
        options = ["add", "empty", "join", "leave", "list", "modify", "remove"] if index == 0 else []
    elif command == "datapack":
        options = ["enable", "disable", "list"]
    elif command == "save-all":
        options = ["flush"]
    return [item for item in _unique_sorted(options) if item.casefold().startswith(partial.casefold())][:20]


def replace_minecraft_completion(value: str, completion: str) -> str:
    if not value or value.endswith(" "):
        return f"{value}{completion}"
    before, separator, _current = value.rpartition(" ")
    return f"{before}{separator}{completion}" if separator else completion


class MinecraftCommandInput(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, candidates: Callable[[str], list[str]], submit: Callable[[str], None], history: list[str] | None = None) -> None:
        super().__init__(parent)
        self._candidates = candidates
        self._submit = submit
        self._matches: list[str] = []
        self._history = list(history or [])[-200:]
        self._history_index: int | None = None
        self._visible = False
        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Label(row, text=">").pack(side="left", padx=(0, 6))
        self.entry = enable_clipboard_paste(ttk.Entry(row))
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Отправить", command=self.submit).pack(side="left", padx=(8, 0))
        self.listbox = tk.Listbox(self, height=7, exportselection=False, activestyle="none")
        self.entry.bind("<KeyRelease>", self._on_key_release, add="+")
        self.entry.bind("<Tab>", self._on_tab)
        self.entry.bind("<Return>", lambda _event: self.submit() or "break")
        self.entry.bind("<Up>", lambda _event: self._move(-1) or "break")
        self.entry.bind("<Down>", lambda _event: self._move(1) or "break")
        self.entry.bind("<Escape>", lambda _event: self._hide() or "break")
        self.listbox.bind("<Double-Button-1>", lambda _event: self._apply_selected())

    @property
    def history(self) -> list[str]:
        return list(self._history)

    def submit(self) -> None:
        value = self.entry.get().strip()
        if not value:
            return
        if not self._history or self._history[-1] != value:
            self._history.append(value)
            self._history = self._history[-200:]
        self._history_index = None
        self.entry.delete(0, "end")
        self._hide()
        self._submit(value)
        self.entry.focus_set()

    def _on_key_release(self, event: tk.Event) -> None:
        if event.keysym not in {"Tab", "Return", "Up", "Down", "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R"}:
            self._history_index = None
            self._refresh()

    def _on_tab(self, _event: tk.Event) -> str:
        if not self._visible:
            self._refresh()
        self._apply_selected()
        return "break"

    def _refresh(self) -> None:
        self._matches = self._candidates(self.entry.get())
        if not self._matches:
            self._hide()
            return
        self.listbox.delete(0, "end")
        for item in self._matches:
            self.listbox.insert("end", item)
        self.listbox.selection_set(0)
        if not self._visible:
            self.listbox.pack(fill="x", pady=(4, 0))
            self._visible = True

    def _hide(self) -> None:
        if self._visible:
            self.listbox.pack_forget()
        self._visible = False
        self._matches = []

    def _move(self, direction: int) -> None:
        if self._visible and self._matches:
            current = int(self.listbox.curselection()[0]) if self.listbox.curselection() else 0
            target = (current + direction) % len(self._matches)
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(target)
            self.listbox.see(target)
            return
        if not self._history:
            return
        if self._history_index is None:
            self._history_index = len(self._history)
        self._history_index = max(0, min(len(self._history), self._history_index + direction))
        value = "" if self._history_index == len(self._history) else self._history[self._history_index]
        self.entry.delete(0, "end")
        self.entry.insert(0, value)

    def _apply_selected(self) -> None:
        if not self._matches:
            return
        index = int(self.listbox.curselection()[0]) if self.listbox.curselection() else 0
        current = self.entry.get()
        self.entry.delete(0, "end")
        self.entry.insert(0, replace_minecraft_completion(current, self._matches[index]))
        self.entry.icursor("end")
        self._refresh()


def is_rcon_lifecycle_message(message: str) -> bool:
    lower = message.casefold()
    return (
        any(subject in lower for subject in ("rcon client", "rcon listener", "rcon connection"))
        and any(marker in lower for marker in (" started", " shutting down", " stopped", " disconnected", " connection closed", " accepted connection"))
    )


def is_legacy_agent_network_error(message: str) -> bool:
    """Recognize only repetitive transport errors emitted by old Agents."""

    value = str(message)
    lower = value.casefold()
    legacy_prefix = value.startswith("[agent] Ошибка:") or "agent tick failed:" in lower
    network_marker = any(
        marker in lower
        for marker in (
            "hub unavailable",
            "temporary failure in name resolution",
            "name or service not known",
            "read operation timed out",
            "urlopen error",
            "connection timed out",
        )
    )
    return legacy_prefix and network_marker


class ConsoleView(ttk.Frame):
    LEVELS = ("ALL", "INFO", "WARN", "ERROR", "DEBUG")

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.events: list[dict[str, Any]] = []
        self.auto_scroll = tk.BooleanVar(value=True)
        self.level = tk.StringVar(value="ALL")
        self.search = tk.StringVar()
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(toolbar, text="Уровень:").pack(side="left")
        level_box = ttk.Combobox(toolbar, textvariable=self.level, values=self.LEVELS, state="readonly", width=9)
        level_box.pack(side="left", padx=(6, 12))
        ttk.Label(toolbar, text="Поиск:").pack(side="left")
        search_entry = enable_clipboard_paste(ttk.Entry(toolbar, textvariable=self.search, width=30))
        search_entry.pack(side="left", fill="x", expand=True, padx=(6, 12))
        ttk.Checkbutton(toolbar, text="Автопрокрутка", variable=self.auto_scroll).pack(side="left")
        ttk.Button(toolbar, text="Очистить локально", command=self.clear_local).pack(side="left", padx=(10, 0))
        self.text = tk.Text(
            self, wrap="none", state="disabled", background="#0d141a", foreground="#d7e1e9",
            insertbackground="#ffffff", selectbackground="#315a7d", font=("Cascadia Mono", 9), undo=False,
        )
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.text.pack(side="left", fill="both", expand=True)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        self.text.tag_configure("INFO", foreground="#d7e1e9")
        self.text.tag_configure("WARN", foreground="#ffd166")
        self.text.tag_configure("ERROR", foreground="#ff6b6b")
        self.text.tag_configure("DEBUG", foreground="#7f8c98")
        self.text.tag_configure("COMMAND", foreground="#63e6be")
        self.level.trace_add("write", lambda *_args: self.render())
        self.search.trace_add("write", lambda *_args: self._schedule_render())
        self._render_after: str | None = None

    def _schedule_render(self) -> None:
        if self._render_after:
            self.after_cancel(self._render_after)
        self._render_after = self.after(180, self.render)

    @staticmethod
    def _level(event: dict[str, Any]) -> str:
        explicit = str(event.get("level", "")).upper()
        if explicit in {"INFO", "WARN", "ERROR", "DEBUG"}:
            return explicit
        lower = str(event.get("message", "")).casefold()
        if any(item in lower for item in ("error", "fatal", "exception", "failed", "ошибка")):
            return "ERROR"
        if any(item in lower for item in ("warn", "warning", "предупреж")):
            return "WARN"
        if "debug" in lower or "trace" in lower:
            return "DEBUG"
        return "INFO"

    def append(self, events: list[dict[str, Any]]) -> None:
        visible_new: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            message = str(event.get("message", "")).strip()
            if not message or is_rcon_lifecycle_message(message) or is_legacy_agent_network_error(message):
                continue
            normalized = {**event, "message": message, "level": self._level(event), "repeat": 1}
            if self.events and all(self.events[-1].get(key) == normalized.get(key) for key in ("message", "level", "instance_id", "source")):
                self.events[-1]["repeat"] = int(self.events[-1].get("repeat", 1)) + 1
                self.render()
                continue
            self.events.append(normalized)
            visible_new.append(normalized)
        if len(self.events) > 5000:
            self.events = self.events[-4000:]
            self.render()
            return
        if visible_new:
            self._append_rendered(visible_new)

    def _matches(self, event: dict[str, Any]) -> bool:
        level = self.level.get()
        needle = self.search.get().casefold().strip()
        return (level == "ALL" or event.get("level") == level) and (not needle or needle in str(event.get("message", "")).casefold())

    def _format(self, event: dict[str, Any]) -> str:
        timestamp = event.get("created_at")
        stamp = ""
        if timestamp:
            try:
                stamp = dt.datetime.fromtimestamp(int(timestamp) / 1000).strftime("%H:%M:%S") + " "
            except (OSError, TypeError, ValueError):
                pass
        instance = f"[{event.get('instance_id')}] " if event.get("instance_id") else ""
        repeat = int(event.get("repeat", 1))
        suffix = f"  ×{repeat}" if repeat > 1 else ""
        return f"{stamp}{instance}{event.get('message', '')}{suffix}\n"

    def _append_rendered(self, events: list[dict[str, Any]]) -> None:
        try:
            at_bottom = float(self.text.yview()[1]) >= 0.995
        except tk.TclError:
            at_bottom = True
        self.text.configure(state="normal")
        for event in events:
            if not self._matches(event):
                continue
            tag = "COMMAND" if str(event.get("message", "")).startswith((">", "▶", "[RCON]")) else str(event.get("level", "INFO"))
            self.text.insert("end", self._format(event), tag)
        self.text.configure(state="disabled")
        if self.auto_scroll.get() and at_bottom:
            self.text.see("end")

    def render(self) -> None:
        self._render_after = None
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._append_rendered(self.events)

    def clear_local(self) -> None:
        self.events.clear()
        self.render()

    def copy_all(self) -> None:
        text = "".join(self._format(event) for event in self.events if self._matches(event))
        self.clipboard_clear()
        self.clipboard_append(text)


class TextEditor(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, save: Callable[[str], None], writable: bool = True) -> None:
        super().__init__(parent)
        self._save_callback = save
        self.writable = writable
        self._last_saved = ""
        self.path = ""
        self.encoding = "utf-8"
        self.mtime_ns: int | None = None
        self.dirty = tk.BooleanVar(value=False)
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        self.path_var = tk.StringVar(value="Файл не открыт")
        ttk.Label(toolbar, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Label(toolbar, textvariable=self.dirty, foreground="#ffb703").pack(side="left", padx=8)
        if writable:
            ttk.Button(toolbar, text="↶", width=3, command=lambda: self.text.event_generate("<<Undo>>")).pack(side="right", padx=(4, 0))
            ttk.Button(toolbar, text="↷", width=3, command=lambda: self.text.event_generate("<<Redo>>")).pack(side="right", padx=(4, 0))
            ttk.Button(toolbar, text="Сохранить  Ctrl+S", command=self.save).pack(side="right")
        else:
            ttk.Label(toolbar, text="Только чтение", style="Subtle.TLabel").pack(side="right")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.lines = tk.Text(body, width=5, padx=6, takefocus=False, background="#151c22", foreground="#6c7a86", state="disabled", font=("Cascadia Mono", 10), wrap="none")
        self.text = tk.Text(body, undo=True, maxundo=200, background="#0d141a", foreground="#d7e1e9", insertbackground="#ffffff", selectbackground="#315a7d", font=("Cascadia Mono", 10), wrap="none")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self._scroll)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=lambda first, last: self._on_scroll(first, last, scrollbar), xscrollcommand=horizontal.set)
        self.lines.pack(side="left", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        horizontal.pack(fill="x")
        self.text.bind("<<Modified>>", self._modified)
        self.text.bind("<Control-s>", lambda _event: self.save() or "break")
        self.text.bind("<Control-S>", lambda _event: self.save() or "break")
        self.text.bind("<Control-f>", self._find_dialog)
        self.text.tag_configure("key", foreground="#74c0fc")
        self.text.tag_configure("string", foreground="#a9e34b")
        self.text.tag_configure("comment", foreground="#7f8c98")
        self.text.tag_configure("number", foreground="#ffd43b")
        self._highlight_after: str | None = None

    def load(self, *, path: str, content: str, encoding: str, mtime_ns: int | None) -> None:
        self.path = path
        self.encoding = encoding
        self.mtime_ns = mtime_ns
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self._last_saved = content
        self.dirty.set(False)
        self.path_var.set(f"{path} · {encoding}")
        self._update_lines()
        self._highlight()
        if not self.writable:
            self.text.configure(state="disabled")

    def content(self) -> str:
        return self.text.get("1.0", "end-1c")

    def save(self) -> None:
        if not self.path or not self.writable:
            return
        self._save_callback(self.content())

    def mark_saved(self, mtime_ns: int | None) -> None:
        self.mtime_ns = mtime_ns
        self._last_saved = self.content()
        self.text.edit_modified(False)
        self.dirty.set(False)

    def confirm_discard(self) -> bool:
        return not self.dirty.get() or messagebox.askyesno("Несохранённые изменения", "Закрыть файл без сохранения?")

    def clear_file(self) -> None:
        self.path = ""
        self.encoding = "utf-8"
        self.mtime_ns = None
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.edit_reset()
        self.text.edit_modified(False)
        self._last_saved = ""
        self.dirty.set(False)
        self.path_var.set("Файл не открыт")
        self._update_lines()
        if not self.writable:
            self.text.configure(state="disabled")

    def _modified(self, _event: tk.Event) -> None:
        if not self.text.edit_modified():
            return
        self.dirty.set(self.content() != self._last_saved)
        self.text.edit_modified(False)
        self._update_lines()
        if self._highlight_after:
            self.after_cancel(self._highlight_after)
        self._highlight_after = self.after(250, self._highlight)

    def _update_lines(self) -> None:
        count = int(self.text.index("end-1c").split(".")[0])
        value = "\n".join(str(index) for index in range(1, count + 1))
        self.lines.configure(state="normal")
        self.lines.delete("1.0", "end")
        self.lines.insert("1.0", value)
        self.lines.configure(state="disabled")

    def _on_scroll(self, first: str, last: str, scrollbar: ttk.Scrollbar) -> None:
        scrollbar.set(first, last)
        self.lines.yview_moveto(first)

    def _scroll(self, *args: Any) -> None:
        self.text.yview(*args)
        self.lines.yview(*args)

    def _highlight(self) -> None:
        self._highlight_after = None
        content = self.content()
        for tag in ("key", "string", "comment", "number"):
            self.text.tag_remove(tag, "1.0", "end")
        patterns = [
            ("comment", re.compile(r"(?m)^\s*[#;].*$|//.*$")),
            ("string", re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')),
            ("number", re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")),
            ("key", re.compile(r"(?m)^\s*([A-Za-z0-9_.-]+)\s*(?==|:)")),
        ]
        for tag, pattern in patterns:
            for match in pattern.finditer(content):
                start = f"1.0+{match.start(1) if match.lastindex else match.start()}c"
                end = f"1.0+{match.end(1) if match.lastindex else match.end()}c"
                self.text.tag_add(tag, start, end)

    def _find_dialog(self, _event: tk.Event | None = None) -> str:
        dialog = tk.Toplevel(self)
        dialog.title("Поиск")
        dialog.transient(self.winfo_toplevel())
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        query = enable_clipboard_paste(ttk.Entry(frame, width=36))
        query.pack(side="left", fill="x", expand=True)

        def find_next() -> None:
            needle = query.get()
            if not needle:
                return
            start = self.text.index("insert +1c")
            found = self.text.search(needle, start, stopindex="end", nocase=True)
            if not found:
                found = self.text.search(needle, "1.0", stopindex=start, nocase=True)
            if found:
                end = f"{found}+{len(needle)}c"
                self.text.tag_remove("sel", "1.0", "end")
                self.text.tag_add("sel", found, end)
                self.text.mark_set("insert", end)
                self.text.see(found)

        ttk.Button(frame, text="Далее", command=find_next).pack(side="left", padx=(8, 0))
        query.bind("<Return>", lambda _e: find_next())
        query.focus_set()
        return "break"


class MetricCard(ttk.Frame):
    """Dark dashboard metric with a compact live graph or usage bar."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        icon: str = "◆",
        accent: str = "#2f80ff",
        mode: str = "bar",
    ) -> None:
        super().__init__(parent, style="Card.TFrame", padding=15, height=182)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self.accent = accent
        self.mode = mode
        self.history: list[float] = []
        self._sample_id: Any = object()
        self.value = tk.StringVar(value="—")
        self.detail = tk.StringVar(value="")
        self.progress: float | None = None

        header = ttk.Frame(self, style="Surface.TFrame")
        header.pack(fill="x")
        badge = tk.Label(
            header,
            text=icon,
            background="#10233a",
            foreground=accent,
            font=("Segoe UI Symbol", 14, "bold"),
            padx=8,
            pady=5,
        )
        badge.pack(side="left")
        ttk.Label(header, text=title, style="MetricTitle.TLabel").pack(side="left", padx=(10, 0))
        ttk.Label(self, textvariable=self.value, style="MetricValue.TLabel").pack(anchor="w", pady=(14, 0))
        ttk.Label(self, textvariable=self.detail, style="MetricDetail.TLabel").pack(anchor="w", pady=(3, 7))
        self.graph = tk.Canvas(self, height=46, background="#0c1724", highlightthickness=0)
        self.graph.pack(side="bottom", fill="x")
        self.graph.bind("<Configure>", lambda _event: self._draw())

    def set(
        self,
        value: str,
        *,
        detail: str = "",
        progress: float | None = None,
        sample_id: Any = None,
    ) -> None:
        self.value.set(value)
        self.detail.set(detail)
        self.progress = None if progress is None else max(0.0, min(100.0, float(progress)))
        if self.mode == "line" and self.progress is not None and sample_id != self._sample_id:
            self.history.append(self.progress)
            self.history = self.history[-28:]
            self._sample_id = sample_id
        self.after_idle(self._draw)

    def _draw(self) -> None:
        try:
            width = max(20, self.graph.winfo_width())
            height = max(20, self.graph.winfo_height())
        except tk.TclError:
            return
        self.graph.delete("all")
        if self.mode == "line":
            for fraction in (0.25, 0.5, 0.75):
                y = round(height * fraction)
                self.graph.create_line(0, y, width, y, fill="#17283a", dash=(2, 4))
            values = self.history or ([self.progress] if self.progress is not None else [])
            if len(values) == 1:
                values = [values[0], values[0]]
            if values:
                points: list[float] = []
                for index, item in enumerate(values):
                    x = index * (width - 2) / max(1, len(values) - 1) + 1
                    y = height - 3 - item * (height - 7) / 100
                    points.extend((x, y))
                polygon = [1, height - 2, *points, width - 1, height - 2]
                self.graph.create_polygon(polygon, fill="#102541", outline="")
                self.graph.create_line(*points, fill=self.accent, width=2, smooth=True)
            return
        self.graph.create_rectangle(0, 15, width, 31, fill="#172333", outline="#26364a")
        if self.progress is not None:
            filled = max(2, round(width * self.progress / 100))
            self.graph.create_rectangle(1, 16, filled, 30, fill=self.accent, outline=self.accent)


class CommandPalette(tk.Toplevel):
    def __init__(self, parent: tk.Misc, actions: list[tuple[str, Callable[[], None]]]) -> None:
        super().__init__(parent)
        self.title("Команды")
        self.transient(parent.winfo_toplevel())
        self.geometry("600x420")
        self.actions = actions
        self.filtered = actions
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        self.query = tk.StringVar()
        entry = enable_clipboard_paste(ttk.Entry(frame, textvariable=self.query, font=("Segoe UI", 12)))
        entry.pack(fill="x", pady=(0, 10))
        self.listbox = tk.Listbox(frame, activestyle="none", font=("Segoe UI", 11))
        self.listbox.pack(fill="both", expand=True)
        self.query.trace_add("write", lambda *_args: self.refresh())
        entry.bind("<Return>", lambda _event: self.execute())
        entry.bind("<Down>", lambda _event: self._move(1) or "break")
        entry.bind("<Up>", lambda _event: self._move(-1) or "break")
        self.listbox.bind("<Double-Button-1>", lambda _event: self.execute())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.refresh()
        self.grab_set()
        entry.focus_set()

    def refresh(self) -> None:
        words = self.query.get().casefold().split()
        self.filtered = [item for item in self.actions if all(word in item[0].casefold() for word in words)]
        self.listbox.delete(0, "end")
        for label, _callback in self.filtered:
            self.listbox.insert("end", label)
        if self.filtered:
            self.listbox.selection_set(0)

    def _move(self, direction: int) -> None:
        if not self.filtered:
            return
        current = int(self.listbox.curselection()[0]) if self.listbox.curselection() else 0
        target = max(0, min(len(self.filtered) - 1, current + direction))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(target)
        self.listbox.see(target)

    def execute(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        callback = self.filtered[int(selection[0])][1]
        self.destroy()
        callback()


class TransferProgress(tk.Toplevel):
    """Thread-safe pause/cancel surface used by upload and download jobs."""

    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.updates: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self.progress = tk.DoubleVar(value=0)
        self.message = tk.StringVar(value="Подготовка…")
        self.metrics = tk.StringVar(value="")
        self.pause_text = tk.StringVar(value="Пауза")
        self._sample_stage = ""
        self._sample_bytes = 0
        self._sample_time = time.monotonic()
        self._speed_bytes = 0.0
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.message, wraplength=480).pack(anchor="w", pady=(8, 6))
        ttk.Progressbar(frame, maximum=100, variable=self.progress, length=480).pack(fill="x")
        ttk.Label(frame, textvariable=self.metrics, style="Subtle.TLabel").pack(anchor="w", pady=(5, 0))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, textvariable=self.pause_text, command=self.toggle_pause).pack(side="left")
        ttk.Button(actions, text="Отмена", command=self.cancel).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.after(100, self._poll_updates)

    def update_job(self, job: dict[str, Any]) -> None:
        self.updates.put(dict(job))

    def _poll_updates(self) -> None:
        latest: dict[str, Any] | None = None
        while True:
            try:
                latest = self.updates.get_nowait()
            except queue.Empty:
                break
        if latest:
            self.progress.set(max(0, min(100, float(latest.get("progress", 0) or 0))))
            self.message.set(str(latest.get("message") or latest.get("stage") or "Выполняется…"))
            stage = str(latest.get("stage") or "")
            try:
                transferred = max(0, int(latest.get("transferred_bytes")))
                total = max(0, int(latest.get("total_bytes")))
            except (TypeError, ValueError):
                transferred = total = 0
            if stage in {"upload", "download"} and total:
                now = time.monotonic()
                if stage != self._sample_stage or transferred < self._sample_bytes:
                    self._sample_stage = stage
                    self._sample_bytes = transferred
                    self._sample_time = now
                    self._speed_bytes = 0.0
                elif transferred > self._sample_bytes and now > self._sample_time:
                    instant = (transferred - self._sample_bytes) / (now - self._sample_time)
                    self._speed_bytes = instant if self._speed_bytes <= 0 else self._speed_bytes * 0.65 + instant * 0.35
                    self._sample_bytes = transferred
                    self._sample_time = now
                remaining = max(0, total - transferred)
                eta = remaining / self._speed_bytes if self._speed_bytes > 0 else None
                eta_text = f" · осталось {display_duration(eta)}" if eta is not None else ""
                self.metrics.set(
                    f"{display_bytes(transferred)} / {display_bytes(total)} · "
                    f"{display_bytes(self._speed_bytes, per_second=True) if self._speed_bytes > 0 else 'скорость…'}{eta_text}"
                )
            elif stage == "hash" and total:
                self.metrics.set(f"Проверено {display_bytes(transferred)} / {display_bytes(total)}")
            elif stage in {"complete", "completed"}:
                self.metrics.set("Готово")
        try:
            self.after(100, self._poll_updates)
        except tk.TclError:
            pass

    def toggle_pause(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_text.set("Пауза")
        else:
            self.pause_event.set()
            self.pause_text.set("Продолжить")
            self.message.set("Передача приостановлена")

    def cancel(self) -> None:
        self.cancel_event.set()
        self.pause_event.clear()
        self.message.set("Отменяю…")

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def paused(self) -> bool:
        return self.pause_event.is_set()

    def finish(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass
