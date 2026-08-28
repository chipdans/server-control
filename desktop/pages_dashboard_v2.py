"""Compact status dashboard for the home server."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from pages_base import BasePage
from widgets import MetricCard, display_bytes, display_duration, display_percent


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class StateCard(ttk.LabelFrame):
    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent, text=title, padding=14)
        self.value = tk.StringVar(value="Проверяю…")
        self.detail = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.value, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(self, textvariable=self.detail, style="Subtle.TLabel", wraplength=330).pack(anchor="w", pady=(5, 0))

    def set(self, value: str, detail: str = "") -> None:
        self.value.set(value)
        self.detail.set(detail)


class DashboardPage(BasePage):
    page_id = "dashboard"
    title = "Состояние"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        summary = ttk.Frame(self)
        summary.pack(fill="x")
        self.hub = StateCard(summary, "Приложение")
        self.power = StateCard(summary, "Питание сервера")
        self.server = StateCard(summary, "Домашний сервер")
        self.minecraft = StateCard(summary, "Minecraft · Dragonfyre")
        for column, card in enumerate((self.hub, self.power, self.server, self.minecraft)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 3 else 5))
            summary.columnconfigure(column, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(10, 12))
        if panel.state.has_permission("server.power") or panel.state.has_permission("power_control"):
            ttk.Button(actions, text="Включить питание", command=lambda: panel.power_action(True)).pack(side="left")
            ttk.Button(actions, text="Безопасно выключить", style="Danger.TButton", command=lambda: panel.power_action(False)).pack(side="left", padx=7)
        if panel.state.has_permission("terminal.linux") or panel.state.has_permission("terminal.minecraft"):
            ttk.Button(actions, text="Открыть консоли", command=lambda: panel.select_page("console")).pack(side="right")
        ttk.Button(actions, text="Обновить сейчас", command=panel.refresh_now).pack(side="right", padx=7)

        metrics = ttk.Frame(self)
        metrics.pack(fill="x")
        self.cpu = MetricCard(metrics, "CPU")
        self.memory = MetricCard(metrics, "Оперативная память")
        self.disk = MetricCard(metrics, "Диск / ")
        self.temperature = MetricCard(metrics, "Температура и аптайм")
        for column, card in enumerate((self.cpu, self.memory, self.disk, self.temperature)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 3 else 5))
            metrics.columnconfigure(column, weight=1)

        self.info = tk.StringVar(value="Ожидаю данные Agent…")
        ttk.Label(self, textvariable=self.info, style="Subtle.TLabel", wraplength=1050).pack(anchor="w", pady=(14, 0))

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        state = self.panel.state
        envelope = mapping(state.server)
        status = mapping(envelope.get("status"))
        host = mapping(status.get("server"))
        metrics = mapping(host.get("metrics"))

        if state.connected:
            latency = f"{state.latency_ms} мс" if state.latency_ms is not None else "ответ получен"
            self.hub.set("Подключено", f"Control Hub · {latency}")
        else:
            self.hub.set("Нет связи", state.last_error or "Повторное подключение…")

        power = mapping(state.power)
        if power.get("on") is True:
            self.power.set("Включено", "Умная розетка доступна" if power.get("online") is not False else "Последнее известное состояние")
        elif power.get("on") is False:
            self.power.set("Выключено", "Домашний сервер обесточен")
        else:
            self.power.set("Неизвестно", "Состояние розетки ещё не получено")

        online = bool(envelope.get("online"))
        hostname = str(host.get("hostname") or "ChipdanServer")
        if online:
            age = max(0, int((envelope.get("age_ms") or 0) / 1000))
            self.server.set("Работает", f"{hostname} · Agent отвечал {age} с назад")
        else:
            self.server.set("Не отвечает", f"{hostname} · показаны последние известные данные")

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
            "CRASHED": "Ошибка запуска",
        }.get(minecraft_state.upper(), minecraft_state or "Неизвестно")
        players = mapping(instance.get("players"))
        detail = f"Игроков: {players.get('online', '—')}/{players.get('max', '—')}"
        startup = mapping(instance.get("startup"))
        if startup.get("label"):
            detail += f" · {startup.get('label')} {startup.get('progress', 0)}%"
        self.minecraft.set(minecraft_label, detail)

        cpu = mapping(metrics.get("cpu"))
        cpu_percent, cpu_text = display_percent(cpu.get("percent"))
        loads = cpu.get("load_average") if isinstance(cpu.get("load_average"), list) else []
        self.cpu.set(cpu_text, detail="load: " + (" / ".join(str(value) for value in loads) if loads else "—"), progress=cpu_percent)
        memory = mapping(metrics.get("memory"))
        mem_percent, mem_text = display_percent(memory.get("percent"))
        self.memory.set(mem_text, detail=f"{display_bytes(memory.get('used_bytes'))} из {display_bytes(memory.get('total_bytes'))}", progress=mem_percent)
        filesystem = mapping(metrics.get("filesystem"))
        disk_percent, disk_text = display_percent(filesystem.get("percent"))
        self.disk.set(disk_text, detail=f"свободно {display_bytes(filesystem.get('available_bytes'))}", progress=disk_percent)
        temperature = metrics.get("temperature_celsius")
        temperature_text = f"{temperature} °C" if temperature is not None else "Датчик недоступен"
        self.temperature.set(temperature_text, detail=f"аптайм {display_duration(metrics.get('uptime_seconds'))}", progress=None)

        addresses = mapping(status.get("system")).get("ip_addresses")
        agent_version = status.get("agent_version", "—")
        self.info.set(
            f"Agent {agent_version} · IP: {', '.join(str(value) for value in addresses) if isinstance(addresses, list) and addresses else '—'} · "
            "состояние обновляется каждую секунду, когда окно приложения открыто."
        )
