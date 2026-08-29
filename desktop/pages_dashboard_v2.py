"""Compact status dashboard for the home server."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from pages_base import BasePage
from widgets import MetricCard, display_bytes, display_duration, display_percent, numeric_value


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class StateCard(ttk.Frame):
    def __init__(self, parent: tk.Misc, title: str, *, icon: str, accent: str) -> None:
        super().__init__(parent, style="Card.TFrame", padding=18, height=206)
        self.grid_propagate(False)
        self.value = tk.StringVar(value="Проверяю…")
        self.detail = tk.StringVar(value="")
        self.progress = tk.DoubleVar(value=0)
        ttk.Label(self, text=title, style="CardTitle.TLabel").pack(anchor="w")
        badge = tk.Label(
            self,
            text=icon,
            background="#10233a",
            foreground=accent,
            font=("Segoe UI Symbol", 19, "bold"),
            padx=10,
            pady=7,
        )
        badge.pack(anchor="w", pady=(15, 11))
        self.value_label = ttk.Label(self, textvariable=self.value, style="StateNeutral.TLabel")
        self.value_label.pack(anchor="w")
        ttk.Label(self, textvariable=self.detail, style="SurfaceSubtle.TLabel", wraplength=260).pack(anchor="w", pady=(7, 0))
        self.bar = ttk.Progressbar(self, maximum=100, variable=self.progress, style="Purple.Horizontal.TProgressbar")

    def set(self, value: str, detail: str = "", *, tone: str = "neutral", progress: float | None = None) -> None:
        self.value.set(value)
        self.detail.set(detail)
        styles = {
            "success": "StateSuccess.TLabel",
            "danger": "StateDanger.TLabel",
            "accent": "StateAccent.TLabel",
            "purple": "StatePurple.TLabel",
            "warning": "StateWarning.TLabel",
        }
        self.value_label.configure(style=styles.get(tone, "StateNeutral.TLabel"))
        if progress is None:
            self.bar.pack_forget()
        else:
            self.progress.set(max(0, min(100, float(progress))))
            if not self.bar.winfo_ismapped():
                self.bar.pack(side="bottom", fill="x", pady=(10, 0))


class DashboardPage(BasePage):
    page_id = "dashboard"
    title = "Состояние"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        summary = ttk.Frame(self)
        summary.pack(fill="x")
        self.hub = StateCard(summary, "Приложение", icon="⌁", accent="#ff545d")
        self.power = StateCard(summary, "Питание сервера", icon="⏻", accent="#62d84e")
        self.server = StateCard(summary, "Домашний сервер", icon="▦", accent="#2f80ff")
        self.minecraft = StateCard(summary, "Minecraft · Dragonfyre", icon="◆", accent="#a767ff")
        for column, card in enumerate((self.hub, self.power, self.server, self.minecraft)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 7, 0 if column == 3 else 7))
            summary.columnconfigure(column, weight=1)
        summary.rowconfigure(0, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(10, 12))
        if panel.state.has_permission("server.power") or panel.state.has_permission("power_control"):
            ttk.Button(actions, text="⏻  Включить питание", style="Success.TButton", command=lambda: panel.power_action(True)).pack(side="left")
            ttk.Button(actions, text="⏻  Безопасно выключить", style="Danger.TButton", command=lambda: panel.power_action(False)).pack(side="left", padx=10)
        if panel.state.has_permission("terminal.linux") or panel.state.has_permission("terminal.minecraft"):
            ttk.Button(actions, text="▣  Открыть консоли", style="Accent.TButton", command=lambda: panel.select_page("console")).pack(side="right")
        ttk.Button(actions, text="↻  Обновить сейчас", style="Accent.TButton", command=panel.refresh_now).pack(side="right", padx=10)

        metrics = ttk.Frame(self)
        metrics.pack(fill="x")
        self.cpu = MetricCard(metrics, "CPU", icon="▦", accent="#2f80ff", mode="line")
        self.memory = MetricCard(metrics, "Оперативная память", icon="▤", accent="#62d84e")
        self.disk = MetricCard(metrics, "Диск /", icon="▱", accent="#2f80ff")
        self.temperature = MetricCard(metrics, "Температура и аптайм", icon="♨", accent="#ff8a1f", mode="line")
        for column, card in enumerate((self.cpu, self.memory, self.disk, self.temperature)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 7, 0 if column == 3 else 7))
            metrics.columnconfigure(column, weight=1)
        metrics.rowconfigure(0, weight=1)

        self.info = tk.StringVar(value="Ожидаю данные Agent…")
        info = ttk.Frame(self, style="Card.TFrame", padding=(15, 11))
        info.pack(fill="x", pady=(14, 0))
        ttk.Label(info, text="ⓘ", style="Surface.TLabel", foreground="#2f80ff", font=("Segoe UI Symbol", 13)).pack(side="left", padx=(0, 10))
        ttk.Label(info, textvariable=self.info, style="SurfaceSubtle.TLabel", wraplength=1050).pack(side="left", fill="x", expand=True)

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        state = self.panel.state
        envelope = mapping(state.server)
        status = mapping(envelope.get("status"))
        host = mapping(status.get("server"))
        metrics = mapping(host.get("metrics"))

        if state.connected:
            latency = f"{state.latency_ms} мс" if state.latency_ms is not None else "ответ получен"
            self.hub.set("Подключено", f"Прямой SSH · {latency}", tone="success")
        else:
            self.hub.set("Нет связи", state.last_error or "Повторное подключение…", tone="danger")

        power = mapping(state.power)
        if power.get("on") is True:
            self.power.set("Включено", "Умная розетка доступна" if power.get("online") is not False else "Последнее известное состояние", tone="success")
        elif power.get("on") is False:
            self.power.set("Выключено", "Домашний сервер обесточен", tone="danger")
        else:
            self.power.set("Неизвестно", "Состояние розетки ещё не получено", tone="warning")

        online = bool(envelope.get("online"))
        hostname = str(host.get("hostname") or "ChipdanServer")
        if online:
            age = max(0, int((envelope.get("age_ms") or 0) / 1000))
            self.server.set("Работает", f"●  {hostname} · Agent отвечал {age} с назад", tone="accent")
        else:
            self.server.set("Не отвечает", f"{hostname} · показаны последние известные данные", tone="danger")

        values = status.get("instances") if isinstance(status.get("instances"), list) else []
        instance = next((mapping(item) for item in values if mapping(item).get("id") == "dragonfyre"), None)
        if not instance and values:
            instance = mapping(values[0])
        if not instance:
            instance = mapping(status.get("minecraft"))
        minecraft_state = str(instance.get("state") or ("RUNNING" if instance.get("active") else "STOPPED"))
        minecraft_label = {
            "RUNNING": "Запущен",
            "STARTING": "Запускается",
            "STOPPING": "Останавливается",
            "STOPPED": "Остановлен",
            "OFFLINE": "Остановлен",
            "CRASHED": "Ошибка запуска",
            "UNKNOWN": "Неизвестно",
        }.get(minecraft_state.upper(), minecraft_state or "Неизвестно")
        players = mapping(instance.get("players"))
        detail = f"Игроков: {players.get('online', '—')}/{players.get('max', '—')}"
        startup = mapping(instance.get("startup"))
        if startup.get("label"):
            detail += f" · {startup.get('label')} {startup.get('progress', 0)}%"
        minecraft_tone = {
            "RUNNING": "success",
            "STARTING": "purple",
            "STOPPING": "warning",
            "CRASHED": "danger",
            "OFFLINE": "danger",
            "STOPPED": "danger",
        }.get(minecraft_state.upper(), "neutral")
        startup_progress = numeric_value(startup.get("progress")) if minecraft_state.upper() == "STARTING" else None
        self.minecraft.set(minecraft_label, detail, tone=minecraft_tone, progress=startup_progress)

        cpu = mapping(metrics.get("cpu"))
        cpu_percent, cpu_text = display_percent(cpu.get("percent"))
        loads = cpu.get("load_average") if isinstance(cpu.get("load_average"), list) else []
        cpu_detail = "Load average: " + (" / ".join(str(value) for value in loads) if loads else "—")
        self.cpu.set(
            cpu_text if cpu.get("percent") is not None else "Сбор данных…",
            detail=cpu_detail,
            progress=cpu_percent if cpu.get("percent") is not None else None,
            sample_id=metrics.get("collected_at"),
        )
        memory = mapping(metrics.get("memory"))
        mem_percent, mem_text = display_percent(memory.get("percent"))
        self.memory.set(mem_text, detail=f"{display_bytes(memory.get('used_bytes'))} из {display_bytes(memory.get('total_bytes'))}", progress=mem_percent)
        filesystem = mapping(metrics.get("filesystem"))
        disk_percent, disk_text = display_percent(filesystem.get("percent"))
        self.disk.set(disk_text, detail=f"свободно {display_bytes(filesystem.get('available_bytes'))}", progress=disk_percent)
        temperature = numeric_value(metrics.get("temperature_celsius"))
        temperature_text = f"{temperature} °C" if temperature is not None else "Датчик недоступен"
        self.temperature.set(
            temperature_text,
            detail=f"Аптайм: {display_duration(metrics.get('uptime_seconds'))}",
            progress=temperature,
            sample_id=metrics.get("collected_at"),
        )

        addresses = mapping(status.get("system")).get("ip_addresses")
        self.info.set(
            f"Прямой SSH · IP: {', '.join(str(value) for value in addresses) if isinstance(addresses, list) and addresses else '—'} · "
            "Agent для сбора состояния не используется; обновление каждые 5 секунд."
        )
