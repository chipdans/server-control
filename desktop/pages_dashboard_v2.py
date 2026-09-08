"""Compact status dashboard for the home server."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from pages_base import BasePage
from widgets import MetricCard, display_bytes, display_duration, display_percent, numeric_value


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def selected_minecraft_status(status: dict[str, Any], fallback_selected_id: str | None = None) -> dict[str, Any]:
    """Select the active profile returned by the latest status poll."""

    values = status.get("instances") if isinstance(status.get("instances"), list) else []
    selected_id = str(status.get("selected_instance_id") or fallback_selected_id or "")
    selected = next(
        (mapping(item) for item in values if str(mapping(item).get("id") or "") == selected_id),
        None,
    )
    if selected:
        return selected
    if values:
        return mapping(values[0])
    return mapping(status.get("minecraft"))


def minecraft_diagnostics(instance: dict[str, Any], metrics: dict[str, Any], online: bool) -> dict[str, Any]:
    """Describe measured tick health without guessing a CPU or mod bottleneck."""

    performance = mapping(instance.get("performance"))
    process = mapping(instance.get("process")) if online else {}
    state = str(instance.get("state") or "UNKNOWN").upper()
    age = numeric_value(performance.get("age_seconds"))
    tps, mspt = numeric_value(performance.get("tps")), numeric_value(performance.get("mspt"))
    fresh = online and state == "RUNNING" and performance.get("status") == "ok" and age is not None and age <= 75
    if not fresh:
        tps, mspt = None, None
    tone = "warning"
    if not online:
        message = "Нет связи с сервером. Новые измерения временно недоступны."
    elif state == "STARTING":
        message = "Сборка запускается. TPS и время тика появятся после загрузки мира."
    elif state == "CRASHED":
        message, tone = "Minecraft завершился с ошибкой. Подробности — в консоли и журнале сборки.", "danger"
    elif state != "RUNNING":
        message = "Minecraft сейчас не работает; скорость симуляции не измеряется."
    elif tps is None or mspt is None:
        messages = {
            "disabled": "Для TPS и времени тика нужен включённый RCON. CPU и память Java измеряются независимо.",
            "unconfigured": "Для TPS задайте порт и пароль RCON в настройках сервера, затем перезапустите Minecraft.",
            "auth_failed": "Сервер отклонил пароль RCON. Проверьте настройки и перезапустите Minecraft после их изменения.",
            "unsupported": "Сервер не отдаёт итоговые TPS/MSPT через команды Forge или NeoForge. CPU и память Java доступны.",
            "timeout": "Minecraft не ответил на запрос TPS вовремя. Это не означает, что сервер остановлен.",
        }
        message = messages.get(str(performance.get("status")), "Свежие TPS/MSPT пока недоступны. CPU и память Java измеряются независимо.")
    elif tps < 18 or mspt > 50:
        message, tone = "Сервер замедляется: обработка мира не укладывается в темп 20 тиков/с. Для поиска причины нужен профиль нагрузки.", "danger"
    elif tps < 19.5 or mspt >= 40:
        message = "Запас производительности небольшой. При дополнительной нагрузке возможны задержки."
    else:
        message, tone = "Скорость симуляции в норме. Одно измерение не исключает короткие зависания между опросами.", "success"
    if online and state == "RUNNING":
        memory_percent = numeric_value(mapping(metrics.get("memory")).get("percent"))
        disk_free = numeric_value(mapping(metrics.get("filesystem")).get("available_bytes"))
        if memory_percent is not None and memory_percent >= 90:
            message += " На компьютере мало свободной памяти."
            tone = "warning" if tone == "success" else tone
        if disk_free is not None and disk_free < 5 * 1024 ** 3:
            message += " На диске осталось меньше 5 ГБ."
            tone = "warning" if tone == "success" else tone
    return {
        "tps": tps, "mspt": mspt, "process": process, "message": message, "tone": tone,
        "age_seconds": age if fresh else None,
        "sample_id": performance.get("measured_at") if fresh else None,
    }


class StateCard(ttk.Frame):
    def __init__(self, parent: tk.Misc, title: str, *, icon: str, accent: str) -> None:
        super().__init__(parent, style="Card.TFrame", padding=18, height=206)
        self.grid_propagate(False)
        self.value = tk.StringVar(value="Проверяю…")
        self.detail = tk.StringVar(value="")
        self.progress = tk.DoubleVar(value=0)
        self.title = tk.StringVar(value=title)
        ttk.Label(self, textvariable=self.title, style="CardTitle.TLabel").pack(anchor="w")
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

    def set_title(self, value: str) -> None:
        self.title.set(value)


class DashboardPage(BasePage):
    page_id = "dashboard"
    title = "Состояние"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        # Keep the dashboard usable at the application's minimum window height.
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#07111d")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        body = ttk.Frame(self.canvas)
        body_id = self.canvas.create_window((0, 0), window=body, anchor="nw")
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(body_id, width=event.width))
        body.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        summary = ttk.Frame(body)
        summary.pack(fill="x")
        self.hub = StateCard(summary, "Приложение", icon="⌁", accent="#ff545d")
        self.power = StateCard(summary, "Питание сервера", icon="⏻", accent="#62d84e")
        self.server = StateCard(summary, "Домашний сервер", icon="▦", accent="#2f80ff")
        self.minecraft = StateCard(summary, "Minecraft · Dragonfyre", icon="◆", accent="#a767ff")
        for column, card in enumerate((self.hub, self.power, self.server, self.minecraft)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 7, 0 if column == 3 else 7))
            summary.columnconfigure(column, weight=1)
        summary.rowconfigure(0, weight=1)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 12))
        if panel.state.has_permission("server.power") or panel.state.has_permission("power_control"):
            ttk.Button(actions, text="⏻  Включить питание", style="Success.TButton", command=lambda: panel.power_action(True)).pack(side="left")
            ttk.Button(actions, text="⏻  Безопасно выключить", style="Danger.TButton", command=lambda: panel.power_action(False)).pack(side="left", padx=10)
        if panel.state.has_permission("terminal.linux") or panel.state.has_permission("terminal.minecraft"):
            ttk.Button(actions, text="▣  Открыть консоли", style="Accent.TButton", command=lambda: panel.select_page("console")).pack(side="right")
        ttk.Button(actions, text="↻  Обновить сейчас", style="Accent.TButton", command=panel.refresh_now).pack(side="right", padx=10)
        if panel.state.has_permission("minecraft.restart"):
            ttk.Button(
                actions,
                text="↻  Перезапустить Minecraft",
                style="Accent.TButton",
                command=panel.restart_minecraft,
            ).pack(side="right")

        tabs = ttk.Notebook(body)
        tabs.pack(fill="x")
        minecraft_metrics = ttk.Frame(tabs, padding=(0, 8, 0, 0))
        metrics = ttk.Frame(tabs, padding=(0, 8, 0, 0))
        tabs.add(minecraft_metrics, text="Minecraft")
        tabs.add(metrics, text="Домашний сервер")
        self.tps = MetricCard(minecraft_metrics, "Скорость · TPS", icon="◆", accent="#62d84e", mode="line", wrap_detail=True, height=204)
        self.mspt = MetricCard(minecraft_metrics, "Время тика", icon="◷", accent="#a767ff", mode="line", wrap_detail=True, height=204)
        self.java_cpu = MetricCard(minecraft_metrics, "CPU Java", icon="▦", accent="#2f80ff", mode="line", wrap_detail=True, height=204)
        self.java_memory = MetricCard(minecraft_metrics, "Память Java", icon="▤", accent="#62d84e", wrap_detail=True, height=204)
        for column, card in enumerate((self.tps, self.mspt, self.java_cpu, self.java_memory)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 7, 0 if column == 3 else 7))
            minecraft_metrics.columnconfigure(column, weight=1, uniform="minecraft_metrics")
        self._diagnostic_session = None
        self.diagnostic_text = tk.StringVar(value="Ожидаю измерения Minecraft…")
        self.diagnostic_label = ttk.Label(
            minecraft_metrics, textvariable=self.diagnostic_text, style="Subtle.TLabel", wraplength=900, padding=(4, 10),
        )
        self.diagnostic_label.grid(row=1, column=0, columnspan=4, sticky="ew")
        minecraft_metrics.bind("<Configure>", lambda event: self.diagnostic_label.configure(wraplength=max(200, event.width - 16)))
        self.cpu = MetricCard(metrics, "CPU", icon="▦", accent="#2f80ff", mode="line")
        self.memory = MetricCard(metrics, "Оперативная память", icon="▤", accent="#62d84e")
        self.disk = MetricCard(metrics, "Диск /", icon="▱", accent="#2f80ff")
        self.temperature = MetricCard(metrics, "Температура и аптайм", icon="♨", accent="#ff8a1f", mode="line")
        for column, card in enumerate((self.cpu, self.memory, self.disk, self.temperature)):
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 7, 0 if column == 3 else 7))
            metrics.columnconfigure(column, weight=1, uniform="host_metrics")
        metrics.rowconfigure(0, weight=1)

        self.info = tk.StringVar(value="Подключаюсь к серверу напрямую по SSH…")
        info = ttk.Frame(body, style="Card.TFrame", padding=(15, 11))
        info.pack(fill="x", pady=(14, 0))
        ttk.Label(info, text="ⓘ", style="Surface.TLabel", foreground="#2f80ff", font=("Segoe UI Symbol", 13)).pack(side="left", padx=(0, 10))
        info_label = ttk.Label(info, textvariable=self.info, style="SurfaceSubtle.TLabel", wraplength=900)
        info_label.pack(side="left", fill="x", expand=True)
        info.bind("<Configure>", lambda event: info_label.configure(wraplength=max(200, event.width - 65)))
        self._bind_scroll(body)
        self.canvas.bind("<MouseWheel>", self._scroll)
        self.canvas.bind("<Button-4>", self._scroll)
        self.canvas.bind("<Button-5>", self._scroll)

    def _bind_scroll(self, widget: tk.Misc) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, self._scroll, add="+")
        for child in widget.winfo_children():
            self._bind_scroll(child)

    def _scroll(self, event: tk.Event) -> str:
        if self.canvas.yview() != (0.0, 1.0):
            step = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
            self.canvas.yview_scroll(step * 3, "units")
        return "break"

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
        if online:
            age = max(0, int((envelope.get("age_ms") or 0) / 1000))
            self.server.set("Работает", f"Отвечал {age} с назад", tone="accent")
        else:
            self.server.set("Не отвечает", "Показаны последние известные данные", tone="danger")

        instance = selected_minecraft_status(status, state.selected_instance_id)
        self.minecraft.set_title(f"Minecraft · {instance.get('name') or instance.get('id') or 'сборка'}")
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
        online_count, maximum_count = players.get("online"), players.get("max")
        detail = (
            f"Игроков: {online_count if online_count is not None else '—'}/{maximum_count if maximum_count is not None else '—'}"
            if online_count is not None or maximum_count is not None else "Число игроков недоступно"
        )
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

        diagnostic = minecraft_diagnostics(instance, metrics, online and state.connected)
        process = mapping(diagnostic["process"])
        session = (instance.get("id"), startup.get("start_id"), process.get("pid"))
        if session != self._diagnostic_session or not online or not state.connected or minecraft_state.upper() != "RUNNING":
            for card in (self.tps, self.mspt, self.java_cpu, self.java_memory):
                card.history.clear()
                card._sample_id = object()
            self._diagnostic_session = session
        tps, mspt = diagnostic["tps"], diagnostic["mspt"]
        sample_id = diagnostic["sample_id"]
        age = diagnostic["age_seconds"]
        age_text = f" · {int(age)} с назад" if age is not None else ""
        for card, value in ((self.tps, tps), (self.mspt, mspt)):
            if value is None:
                card.history.clear()
        self.tps.set(f"{tps:.1f} / 20" if tps is not None else "—", detail="Норма: 20 тиков/с" + age_text, progress=tps * 5 if tps is not None else None, sample_id=sample_id)
        self.mspt.set(f"{mspt:.1f} мс" if mspt is not None else "—", detail="Бюджет тика: 50 мс", progress=mspt, sample_id=sample_id)
        java_percent, java_text = display_percent(process.get("cpu_percent"))
        self.java_cpu.set(
            java_text, detail="Доля общей мощности CPU", progress=java_percent if process.get("cpu_percent") is not None else None,
            sample_id=(session, metrics.get("collected_at")),
        )
        total_memory = numeric_value(mapping(metrics.get("memory")).get("total_bytes"))
        java_bytes = numeric_value(process.get("memory_bytes"))
        self.java_memory.set(
            display_bytes(java_bytes), detail="Занято процессом · не лимит Java",
            progress=java_bytes * 100 / total_memory if java_bytes is not None and total_memory else None,
        )
        self.diagnostic_text.set(diagnostic["message"])
        self.diagnostic_label.configure(foreground={"success": "#62d84e", "warning": "#ffbd4a", "danger": "#ff545d"}[diagnostic["tone"]])

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
            f"IP: {', '.join(str(value) for value in addresses) if isinstance(addresses, list) and addresses else '—'} · "
            "Состояние: каждые 5 секунд · TPS/MSPT: каждые 30 секунд."
        )
