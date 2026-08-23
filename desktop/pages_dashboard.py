"""Overview page with real host and Minecraft health data."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from pages_base import BasePage
from widgets import MetricCard, display_bytes, display_duration, display_percent, numeric_value


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class DashboardPage(BasePage):
    page_id = "dashboard"
    title = "Главная"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.info = tk.StringVar(value="Ожидаю первый отчёт агента…")
        self.minecraft = tk.StringVar(value="Сборка не выбрана")
        self.alert = tk.StringVar(value="")

        ttk.Label(self, textvariable=self.info, style="Subtle.TLabel").pack(anchor="w", pady=(0, 12))
        cards = ttk.Frame(self)
        cards.pack(fill="x")
        self.cpu = MetricCard(cards, "CPU")
        self.memory = MetricCard(cards, "Оперативная память")
        self.disk = MetricCard(cards, "Диск /")
        self.network = MetricCard(cards, "Сеть")
        for column, card in enumerate((self.cpu, self.memory, self.disk, self.network)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 3 else 5))
            cards.columnconfigure(column, weight=1)
        extra_cards = ttk.Frame(self)
        extra_cards.pack(fill="x", pady=(10, 0))
        self.disk_io = MetricCard(extra_cards, "Диск I/O")
        self.swap = MetricCard(extra_cards, "Swap")
        self.temperature = MetricCard(extra_cards, "Температура")
        self.connection = MetricCard(extra_cards, "Связь")
        for column, card in enumerate((self.disk_io, self.swap, self.temperature, self.connection)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 3 else 5))
            extra_cards.columnconfigure(column, weight=1)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(14, 0))
        minecraft = ttk.LabelFrame(body, text="Minecraft", padding=14)
        minecraft.pack(side="left", fill="both", expand=True, padx=(0, 7))
        ttk.Label(minecraft, textvariable=self.minecraft, font=("Segoe UI", 12, "bold"), wraplength=580).pack(anchor="w")
        self.startup = ttk.Progressbar(minecraft, maximum=100)
        self.startup.pack(fill="x", pady=10)
        actions = ttk.Frame(minecraft)
        actions.pack(fill="x")
        for action, title, permission in (
            ("start", "Запустить", "minecraft.start"), ("stop", "Остановить", "minecraft.stop"),
            ("restart", "Перезапустить", "minecraft.restart"), ("kill", "Force kill", "minecraft.kill"),
        ):
            if panel.state.has_permission(permission):
                ttk.Button(actions, text=title, style="Danger.TButton" if action == "kill" else "TButton", command=lambda value=action: panel.instance_action(value)).pack(side="left", padx=(0, 6))
        if panel.state.has_permission("minecraft.console"):
            ttk.Button(actions, text="Консоль", command=lambda: panel.select_page("console")).pack(side="right")

        quick = ttk.LabelFrame(body, text="Быстрые действия", padding=14)
        quick.pack(side="left", fill="both", padx=(7, 0))
        quick_actions: list[tuple[str, Any]] = []
        if panel.state.has_permission("minecraft.backups"):
            quick_actions.append(("Создать backup", panel.create_backup))
        if panel.state.has_permission("minecraft.files.read"):
            quick_actions.append(("Открыть файлы", lambda: panel.select_page("files")))
        if panel.state.has_permission("server.view"):
            quick_actions.append(("Процессы и диски", lambda: panel.select_page("monitoring")))
        quick_actions.extend((("Задачи и передачи", lambda: panel.select_page("jobs")), ("Обновить сейчас", panel.refresh_now)))
        for label, callback in quick_actions:
            ttk.Button(quick, text=label, command=callback, width=23).pack(fill="x", pady=3)
        ttk.Label(self, textvariable=self.alert, style="Warning.TLabel", wraplength=950).pack(anchor="w", pady=(12, 0))

    def update_state(self, _changes: dict[str, Any] | None = None) -> None:
        state = self.panel.state
        server = _mapping(state.server.get("status"))
        host = _mapping(server.get("server"))
        metrics = _mapping(host.get("metrics"))
        self.info.set(
            f"{host.get('hostname', 'Домашний сервер')} · агент {server.get('agent_version', '—')} · "
            f"аптайм {display_duration(metrics.get('uptime_seconds'))}"
            if state.connected else f"Нет связи: {state.last_error or 'ожидаю переподключение'}"
        )
        cpu = _mapping(metrics.get("cpu"))
        cpu_percent, cpu_text = display_percent(cpu.get("percent"))
        loads = cpu.get("load_average") if isinstance(cpu.get("load_average"), list) else []
        self.cpu.set(cpu_text, detail="load: " + (" / ".join(str(value) for value in loads) if loads else "—"), progress=cpu_percent)
        memory = _mapping(metrics.get("memory"))
        mem_percent, mem_text = display_percent(memory.get("percent"))
        self.memory.set(mem_text, detail=f"{display_bytes(memory.get('used_bytes'))} из {display_bytes(memory.get('total_bytes'))}", progress=mem_percent)
        filesystem = _mapping(metrics.get("filesystem"))
        disk_percent, disk_text = display_percent(filesystem.get("percent"))
        self.disk.set(disk_text, detail=f"свободно {display_bytes(filesystem.get('available_bytes'))}", progress=disk_percent)
        network = _mapping(metrics.get("network"))
        self.network.set(
            f"↓ {display_bytes(network.get('rx_per_second'), per_second=True)}",
            detail=f"↑ {display_bytes(network.get('tx_per_second'), per_second=True)}",
            progress=None,
        )
        disk_io = _mapping(metrics.get("disk_io"))
        self.disk_io.set(
            f"↓ {display_bytes(disk_io.get('read_per_second'), per_second=True)}",
            detail=f"↑ {display_bytes(disk_io.get('write_per_second'), per_second=True)}",
            progress=None,
        )
        swap_total = numeric_value(memory.get("swap_total_bytes")) or 0.0
        swap_free = numeric_value(memory.get("swap_free_bytes")) or 0.0
        swap_used = max(0.0, swap_total - swap_free)
        swap_percent = swap_used * 100 / swap_total if swap_total else 0.0
        self.swap.set(display_bytes(swap_used), detail=f"из {display_bytes(swap_total)}", progress=swap_percent if swap_total else None)
        temperature = metrics.get("temperature_celsius")
        self.temperature.set(f"{temperature} °C" if temperature is not None else "недоступно", detail="датчик CPU" if temperature is not None else "Linux не предоставил датчик", progress=float(temperature) if isinstance(temperature, (int, float)) else None)
        addresses = server.get("system", {}).get("ip_addresses", []) if isinstance(server.get("system"), dict) else []
        self.connection.set(
            f"{self.panel.state.latency_ms} мс" if self.panel.state.latency_ms is not None else "—",
            detail=f"Agent: {int((self.panel.state.server.get('age_ms') or 0) / 1000)} с назад" + (f" · {', '.join(addresses)}" if addresses else ""),
            progress=None,
        )
        instance = state.selected_instance()
        if not instance:
            self.minecraft.set("Сборки пока не добавлены")
            self.startup["value"] = 0
            self.alert.set("Агент offline: показаны последние известные данные." if not state.server.get("online") else "")
            return
        startup = _mapping(instance.get("startup"))
        players = _mapping(instance.get("players"))
        progress = int(startup.get("progress", 100 if instance.get("state") == "RUNNING" else 0) or 0)
        self.startup["value"] = max(0, min(100, progress))
        self.minecraft.set(
            f"{instance.get('name', instance.get('id'))}: {instance.get('state', 'UNKNOWN')} · "
            f"{startup.get('label', '')} {progress}% · игроков {players.get('online', '—')}/{players.get('max', '—')}\n"
            f"TPS {instance.get('tps') if instance.get('tps') is not None else '—'} · MSPT {instance.get('mspt') if instance.get('mspt') is not None else '—'} · "
            f"RAM {display_bytes(instance.get('process_memory_bytes'))} · CPU {instance.get('process_cpu_percent', '—')}% · "
            f"PID {instance.get('pid') or '—'} · аптайм {display_duration(instance.get('uptime_seconds'))}\n"
            f"Minecraft {instance.get('minecraft_version', '—')} · {instance.get('loader', '—')} {instance.get('loader_version', '')} · "
            f"сборка {instance.get('pack_version', '—')}"
        )
        warnings: list[str] = []
        if not state.server.get("online"):
            warnings.append("Агент offline: показаны последние известные данные, опасные действия временно недоступны.")
        if instance.get("state") == "CRASHED":
            crash = _mapping(instance.get("crash"))
            warnings.append(str(crash.get("summary") or "Minecraft завершился с ошибкой — откройте журналы."))
        if disk_percent >= 90:
            warnings.append("На системном диске осталось мало места.")
        self.alert.set("  ".join(warnings))
