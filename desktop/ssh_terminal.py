"""Embedded VT-style terminal backed by a direct Paramiko SSH channel."""

from __future__ import annotations

import base64
import codecs
import hashlib
import hmac
import io
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, ttk
from typing import Any, Callable

import paramiko
import pyte


class HostFingerprintPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str) -> None:
        self.expected = expected.strip()

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        del client
        digest = hashlib.sha256(key.asbytes()).digest()
        actual = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
        if not hmac.compare_digest(actual, self.expected):
            raise paramiko.SSHException(
                f"Отпечаток SSH-сервера {hostname} не совпал. Ожидался {self.expected}, получен {actual}."
            )


def load_private_key(value: str) -> paramiko.PKey:
    key_types: tuple[type[paramiko.PKey], ...] = (
        paramiko.Ed25519Key,
        paramiko.RSAKey,
        paramiko.ECDSAKey,
    )
    for key_type in key_types:
        try:
            return key_type.from_private_key(io.StringIO(value))
        except (paramiko.SSHException, ValueError):
            pass
    raise paramiko.SSHException(
        "Закрытый SSH-ключ не удалось прочитать. Поддерживаются OpenSSH Ed25519, RSA и ECDSA."
    )


def connection_targets(credentials: dict[str, Any]) -> list[tuple[str, int]]:
    values: list[tuple[str, int, str]] = []
    raw_targets = credentials.get("targets")
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            try:
                host = str(item["host"])
                port = int(item["port"])
            except (KeyError, TypeError, ValueError):
                continue
            if host and 1 <= port <= 65535 and (host, port, str(item.get("network", ""))) not in values:
                values.append((host, port, str(item.get("network", ""))))
    if not values:
        values.append((str(credentials.get("host", "")), int(credentials.get("port", 0)), "internet"))

    lan = next((value for value in values if value[2] == "lan"), None)
    if lan:
        try:
            probe = socket.create_connection((lan[0], lan[1]), timeout=0.7)
        except OSError:
            pass
        else:
            probe.close()
            values = [lan, *[value for value in values if value != lan]]
            return [(host, port) for host, port, _network in values]
    values.sort(key=lambda value: value[2] != "internet")
    return [(host, port) for host, port, _network in values]


class SshSession:
    def __init__(
        self,
        credentials: dict[str, Any],
        *,
        columns: int,
        rows: int,
        output: Callable[[bytes], None],
        state: Callable[[str, bool], None],
    ) -> None:
        self.credentials = dict(credentials)
        self.columns = columns
        self.rows = rows
        self.output = output
        self.state = state
        self.client: paramiko.SSHClient | None = None
        self.channel: paramiko.Channel | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="server-control-ssh", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        client: paramiko.SSHClient | None = None
        channel: paramiko.Channel | None = None
        try:
            username = str(self.credentials.get("username", ""))
            command = str(self.credentials.get("command", ""))
            fingerprint = str(self.credentials.get("host_key_sha256", ""))
            private_key = str(self.credentials.pop("private_key", ""))
            pkey = load_private_key(private_key)
            private_key = ""
            targets = connection_targets(self.credentials)
            last_connect_error: Exception | None = None
            host, port = "", 0
            for host, port in targets:
                candidate = paramiko.SSHClient()
                candidate.set_missing_host_key_policy(HostFingerprintPolicy(fingerprint))
                self.state(f"Подключение к {host}:{port}…", False)
                try:
                    candidate.connect(
                        hostname=host,
                        port=port,
                        username=username,
                        pkey=pkey,
                        allow_agent=False,
                        look_for_keys=False,
                        timeout=10,
                        banner_timeout=10,
                        auth_timeout=10,
                        channel_timeout=10,
                        compress=True,
                    )
                except Exception as error:
                    last_connect_error = error
                    candidate.close()
                    continue
                client = candidate
                break
            if client is None:
                raise last_connect_error or paramiko.SSHException("Нет доступного SSH-адреса.")
            transport = client.get_transport()
            if not transport or not transport.is_active():
                raise paramiko.SSHException("SSH-транспорт не запустился.")
            transport.set_keepalive(15)
            channel = transport.open_session(timeout=12)
            channel.get_pty(term="xterm-256color", width=self.columns, height=self.rows)
            channel.exec_command(command)
            channel.settimeout(0.25)
            with self.lock:
                self.client = client
                self.channel = channel
            self.state(f"Подключено: {host}:{port}", True)

            while not self.stop_event.is_set():
                received = False
                if channel.recv_ready():
                    data = channel.recv(65_536)
                    if data:
                        self.output(data)
                        received = True
                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(65_536)
                    if data:
                        self.output(data)
                        received = True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if not received:
                    time.sleep(0.02)
            if not self.stop_event.is_set():
                code = channel.recv_exit_status() if channel.exit_status_ready() else 0
                self.state(f"SSH-сессия завершена, код {code}.", False)
        except Exception as error:
            if not self.stop_event.is_set():
                self.state(f"Ошибка SSH: {error}", False)
        finally:
            with self.lock:
                self.channel = None
                self.client = None
            try:
                if channel:
                    channel.close()
            finally:
                if client:
                    client.close()
            self.credentials.clear()

    def send(self, value: str | bytes) -> None:
        payload = value.encode("utf-8") if isinstance(value, str) else value
        with self.lock:
            channel = self.channel
            if channel and not channel.closed:
                try:
                    channel.sendall(payload)
                except (OSError, paramiko.SSHException):
                    pass

    def resize(self, columns: int, rows: int) -> None:
        self.columns = max(40, columns)
        self.rows = max(12, rows)
        with self.lock:
            channel = self.channel
            if channel and not channel.closed:
                try:
                    channel.resize_pty(width=self.columns, height=self.rows)
                except (OSError, paramiko.SSHException):
                    pass

    def close(self) -> None:
        self.stop_event.set()
        with self.lock:
            channel = self.channel
            client = self.client
        try:
            if channel:
                channel.close()
        finally:
            if client:
                client.close()


class EmbeddedTerminal(ttk.Frame):
    KEY_SEQUENCES = {
        "Return": "\r",
        "KP_Enter": "\r",
        "BackSpace": "\x7f",
        "Tab": "\t",
        "Escape": "\x1b",
        "Up": "\x1b[A",
        "Down": "\x1b[B",
        "Right": "\x1b[C",
        "Left": "\x1b[D",
        "Home": "\x1b[H",
        "End": "\x1b[F",
        "Delete": "\x1b[3~",
        "Insert": "\x1b[2~",
        "F1": "\x1bOP",
        "F2": "\x1bOQ",
        "F3": "\x1bOR",
        "F4": "\x1bOS",
        "F5": "\x1b[15~",
        "F6": "\x1b[17~",
        "F7": "\x1b[18~",
        "F8": "\x1b[19~",
        "F9": "\x1b[20~",
        "F10": "\x1b[21~",
        "F11": "\x1b[23~",
        "F12": "\x1b[24~",
    }

    def __init__(self, parent: tk.Misc, *, title: str) -> None:
        super().__init__(parent)
        self.title = title
        self.status_var = tk.StringVar(value="Не подключено")
        self.session: SshSession | None = None
        self.columns = 120
        self.rows = 36
        self.screen = pyte.HistoryScreen(self.columns, self.rows, history=5000, ratio=0.5)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.output_queue: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self.state_queue: queue.SimpleQueue[tuple[str, bool]] = queue.SimpleQueue()
        self.connected = False
        self.closed = False
        self._last_render = ""

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(toolbar, text=title, font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Label(toolbar, textvariable=self.status_var, style="Subtle.TLabel").pack(side="left", padx=12)

        terminal_frame = tk.Frame(self, background="#050b13", highlightthickness=1, highlightbackground="#2b3d54")
        terminal_frame.pack(fill="both", expand=True)
        self.text = tk.Text(
            terminal_frame,
            background="#050b13",
            foreground="#d9e7f7",
            insertbackground="#ffffff",
            selectbackground="#245da3",
            selectforeground="#ffffff",
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=12,
            wrap="none",
            undo=False,
            font=("Cascadia Mono", 10),
            cursor="xterm",
        )
        scrollbar_y = ttk.Scrollbar(terminal_frame, orient="vertical", command=self.text.yview)
        scrollbar_x = ttk.Scrollbar(terminal_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar_y.grid(row=0, column=1, sticky="ns")
        scrollbar_x.grid(row=1, column=0, sticky="ew")
        terminal_frame.rowconfigure(0, weight=1)
        terminal_frame.columnconfigure(0, weight=1)

        self.text.bind("<KeyPress>", self._key_press)
        self.text.bind("<Control-Shift-C>", self._copy)
        self.text.bind("<Control-Shift-c>", self._copy)
        self.text.bind("<Control-Shift-V>", self._paste)
        self.text.bind("<Control-Shift-v>", self._paste)
        self.text.bind("<Control-v>", self._paste)
        self.text.bind("<Button-1>", lambda _event: self.text.focus_set())
        self.text.bind("<Prior>", self._page_up)
        self.text.bind("<Next>", self._page_down)
        self.text.bind("<Configure>", self._resize)
        self.after(30, self._drain)

    def connect(self, credentials: dict[str, Any]) -> None:
        self.disconnect()
        self.screen.reset()
        self._last_render = ""
        self.decoder.reset()
        self.status_var.set("Подготовка SSH…")
        self.session = SshSession(
            credentials,
            columns=self.columns,
            rows=self.rows,
            output=self.output_queue.put,
            state=lambda message, connected: self.state_queue.put((message, connected)),
        )
        self.session.start()
        self.text.focus_set()

    def disconnect(self) -> None:
        if self.session:
            self.session.close()
            self.session = None
        self.connected = False

    def _drain(self) -> None:
        if self.closed:
            return
        dirty = False
        for _index in range(500):
            try:
                data = self.output_queue.get_nowait()
            except queue.Empty:
                break
            self.stream.feed(self.decoder.decode(data))
            dirty = True
        for _index in range(20):
            try:
                message, connected = self.state_queue.get_nowait()
            except queue.Empty:
                break
            self.status_var.set(message)
            self.connected = connected
        if dirty:
            self._render()
        self.after(30, self._drain)

    def _render(self) -> None:
        rendered = "\n".join(line.rstrip() for line in self.screen.display).rstrip() + "\n"
        if rendered == self._last_render:
            return
        self._last_render = rendered
        yview = self.text.yview()
        at_bottom = not yview or yview[1] >= 0.98
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", rendered)
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def _key_press(self, event: tk.Event) -> str:
        if not self.session:
            return "break"
        if event.keysym in {"Prior", "Next"}:
            return "break"
        control = bool(event.state & 0x4)
        if control and event.keysym.lower() == "c":
            try:
                self.text.get("sel.first", "sel.last")
            except tk.TclError:
                self.session.send("\x03")
            else:
                self._copy(event)
            return "break"
        if control and event.keysym.lower() == "v":
            return self._paste(event)
        if control and len(event.keysym) == 1 and event.keysym.isalpha():
            self.session.send(bytes([ord(event.keysym.lower()) - 96]))
            return "break"
        sequence = self.KEY_SEQUENCES.get(event.keysym)
        if sequence is not None:
            self.session.send(sequence)
            return "break"
        if event.char and event.char.isprintable():
            self.session.send(event.char)
        return "break"

    def _copy(self, _event: tk.Event | None = None) -> str:
        try:
            selected = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(selected)
        return "break"

    def _paste(self, _event: tk.Event | None = None) -> str:
        if self.session:
            try:
                value = self.clipboard_get()
            except tk.TclError:
                value = ""
            if value:
                self.session.send(value.replace("\r\n", "\n").replace("\n", "\r"))
        return "break"

    def _page_up(self, _event: tk.Event | None = None) -> str:
        self.screen.prev_page()
        self._last_render = ""
        self._render()
        return "break"

    def _page_down(self, _event: tk.Event | None = None) -> str:
        self.screen.next_page()
        self._last_render = ""
        self._render()
        return "break"

    def _resize(self, event: tk.Event) -> None:
        try:
            font = tkfont.Font(font=self.text.cget("font"))
            columns = max(40, int((event.width - 24) / max(1, font.measure("M"))))
            rows = max(12, int((event.height - 18) / max(1, font.metrics("linespace"))))
        except tk.TclError:
            return
        if (columns, rows) == (self.columns, self.rows):
            return
        self.columns, self.rows = columns, rows
        try:
            self.screen.resize(rows, columns)
        except (TypeError, ValueError):
            pass
        if self.session:
            self.session.resize(columns, rows)

    def close(self) -> None:
        self.closed = True
        self.disconnect()
