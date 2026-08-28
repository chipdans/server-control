"""Simple user, permission and owner-account management."""

from __future__ import annotations

import datetime as dt
import tkinter as tk
import urllib.parse
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from pages_base import BasePage
from widgets import enable_clipboard_paste


PERMISSION_LABELS = (
    ("status.view", "Просмотр состояния сервера"),
    ("terminal.linux", "Linux-консоль с правами администратора"),
    ("terminal.minecraft", "Прямая Minecraft-консоль"),
    ("server.power", "Управление питанием сервера"),
    ("users.manage", "Создание и блокировка пользователей"),
)

PRESETS = {
    "Полный доступ": {key for key, _label in PERMISSION_LABELS},
    "Только Minecraft": {"status.view", "terminal.minecraft"},
    "Только просмотр": {"status.view"},
    "Свои настройки": set(),
}


def timestamp(value: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


class UsersPage(BasePage):
    page_id = "users"
    title = "Пользователи и права"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.users: dict[str, dict[str, Any]] = {}
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="Создать пользователя", command=self.create).pack(side="left")
        ttk.Button(toolbar, text="Изменить логин и права", command=self.edit).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Включить / заблокировать", command=self.toggle).pack(side="left")
        ttk.Button(toolbar, text="Задать новый пароль", command=self.reset_password).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Отозвать сеансы", command=self.revoke).pack(side="left")
        ttk.Button(toolbar, text="Удалить", style="Danger.TButton", command=self.delete).pack(side="right")

        self.tree = ttk.Treeview(self, columns=("access", "permissions", "last_login"), show="tree headings")
        self.tree.heading("#0", text="Логин")
        self.tree.column("#0", width=190)
        for column, label, width in (
            ("access", "Доступ", 120),
            ("permissions", "Выданные права", 670),
            ("last_login", "Последний вход", 155),
        ):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, stretch=column == "permissions")
        self.tree.pack(fill="both", expand=True)
        ttk.Label(
            self,
            text="Владелец всегда имеет все права. Его логин и пароль меняются на странице «Моя учётная запись».",
            style="Subtle.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        def success(result: dict[str, Any]) -> None:
            values = result.get("users") if isinstance(result.get("users"), list) else []
            self.users = {str(item["id"]): item for item in values if isinstance(item, dict) and item.get("id")}
            self.tree.delete(*self.tree.get_children())
            labels = dict(PERMISSION_LABELS)
            for user_id, user in self.users.items():
                permissions = [labels.get(value, value) for value in user.get("permissions", []) if value in labels]
                if user.get("role") == "owner":
                    permissions = ["Все права владельца"]
                self.tree.insert(
                    "", "end", iid=user_id, text=user.get("username"),
                    values=(
                        "включён" if user.get("enabled") else "заблокирован",
                        ", ".join(permissions) or "прав нет",
                        timestamp(user.get("last_login_at")),
                    ),
                )

        self.panel.run_async(
            lambda: self.panel.api.request("GET", "/v1/admin/users"),
            success,
            lambda error: self.panel.status(str(error), error=True),
        )

    def selected(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Пользователи", "Сначала выберите пользователя.")
            return None
        return self.users.get(selection[0])

    def create(self) -> None:
        self.user_dialog(None)

    def edit(self) -> None:
        user = self.selected()
        if not user:
            return
        if user.get("role") == "owner":
            self.panel.select_page("account")
            return
        self.user_dialog(user)

    def user_dialog(self, existing: dict[str, Any] | None) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Изменить пользователя" if existing else "Новый пользователь")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        username = tk.StringVar(value=str(existing.get("username", "")) if existing else "")
        password = tk.StringVar()
        preset = tk.StringVar(value="Свои настройки" if existing else "Только Minecraft")

        ttk.Label(frame, text="Логин").grid(row=0, column=0, sticky="w", pady=5)
        enable_clipboard_paste(ttk.Entry(frame, textvariable=username, width=36)).grid(row=0, column=1, sticky="ew", pady=5)
        if not existing:
            ttk.Label(frame, text="Пароль (от 12 символов)").grid(row=1, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(frame, textvariable=password, show="•", width=36)).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Готовый набор").grid(row=2, column=0, sticky="w", pady=5)
        preset_box = ttk.Combobox(frame, textvariable=preset, values=tuple(PRESETS), state="readonly")
        preset_box.grid(row=2, column=1, sticky="ew", pady=5)

        rights = ttk.LabelFrame(frame, text="Точные права", padding=10)
        rights.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=8)
        current = set(existing.get("permissions", [])) if existing else PRESETS["Только Minecraft"]
        variables = {key: tk.BooleanVar(value=key in current) for key, _label in PERMISSION_LABELS}
        owner = self.panel.state.user.get("role") == "owner"
        actor_permissions = set(self.panel.state.user.get("permissions", []))
        for row, (key, label) in enumerate(PERMISSION_LABELS):
            enabled = owner or key in actor_permissions
            ttk.Checkbutton(rights, text=label, variable=variables[key], state="normal" if enabled else "disabled").grid(row=row, column=0, sticky="w", pady=2)

        def apply_preset(_event: tk.Event | None = None) -> None:
            if preset.get() == "Свои настройки":
                return
            desired = PRESETS[preset.get()]
            for key, variable in variables.items():
                if owner or key in actor_permissions:
                    variable.set(key in desired)

        preset_box.bind("<<ComboboxSelected>>", apply_preset)

        def submit() -> None:
            selected_permissions = [key for key, variable in variables.items() if variable.get()]
            payload: dict[str, Any] = {
                "username": username.get().strip(),
                "role": "admin" if "users.manage" in selected_permissions else "user",
                "permissions": selected_permissions,
            }
            if existing:
                method = "PATCH"
                path = f"/v1/admin/users/{urllib.parse.quote(str(existing['id']), safe='')}"
            else:
                method = "POST"
                path = "/v1/admin/users"
                payload["password"] = password.get()

            def success(_result: dict[str, Any]) -> None:
                dialog.destroy()
                self.refresh()
                self.panel.status("Пользователь сохранён")

            self.panel.run_async(
                lambda: self.panel.api.request(method, path, payload),
                success,
                lambda error: messagebox.showerror("Пользователь", str(error), parent=dialog),
            )

        ttk.Button(frame, text="Сохранить", command=submit).grid(row=4, column=1, sticky="e", pady=(8, 0))
        frame.columnconfigure(1, weight=1)

    def toggle(self) -> None:
        user = self.selected()
        if not user or user.get("role") == "owner":
            return
        enabled = not bool(user.get("enabled"))
        self.panel.run_async(
            lambda: self.panel.api.request(
                "PATCH", f"/v1/admin/users/{urllib.parse.quote(str(user['id']), safe='')}", {"enabled": enabled}
            ),
            lambda _result: self.refresh(),
            lambda error: self.panel.status(str(error), error=True),
        )

    def reset_password(self) -> None:
        user = self.selected()
        if not user:
            return
        if user.get("role") == "owner":
            self.panel.select_page("account")
            return
        password = simpledialog.askstring(
            "Новый пароль", f"Новый пароль для {user.get('username')} (минимум 12 символов)", show="•", parent=self
        )
        if not password:
            return
        self.panel.run_async(
            lambda: self.panel.api.request(
                "POST", f"/v1/admin/users/{urllib.parse.quote(str(user['id']), safe='')}/password", {"password": password}
            ),
            lambda _result: self.panel.status("Пароль изменён, старые сеансы отозваны"),
            lambda error: self.panel.status(str(error), error=True),
        )

    def revoke(self) -> None:
        user = self.selected()
        if not user or user.get("role") == "owner":
            return
        self.panel.run_async(
            lambda: self.panel.api.request(
                "POST", f"/v1/admin/users/{urllib.parse.quote(str(user['id']), safe='')}/revoke", {}
            ),
            lambda _result: self.panel.status("Все сеансы пользователя отозваны"),
            lambda error: self.panel.status(str(error), error=True),
        )

    def delete(self) -> None:
        user = self.selected()
        if not user or user.get("role") == "owner":
            return
        if not messagebox.askyesno("Удаление", f"Удалить пользователя {user.get('username')}?", icon="warning"):
            return
        self.panel.run_async(
            lambda: self.panel.api.request(
                "DELETE", f"/v1/admin/users/{urllib.parse.quote(str(user['id']), safe='')}", {}
            ),
            lambda _result: self.refresh(),
            lambda error: self.panel.status(str(error), error=True),
        )


class AccountPage(BasePage):
    page_id = "account"
    title = "Моя учётная запись"

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        form = ttk.LabelFrame(self, text="Изменение логина и пароля", padding=18)
        form.pack(anchor="nw", fill="x", padx=4, pady=4)
        self.username = tk.StringVar(value=str(panel.state.user.get("username", "")))
        self.current_password = tk.StringVar()
        self.new_password = tk.StringVar()
        self.confirm_password = tk.StringVar()
        rows = (
            ("Новый логин", self.username, False),
            ("Текущий пароль", self.current_password, True),
            ("Новый пароль (необязательно)", self.new_password, True),
            ("Повторите новый пароль", self.confirm_password, True),
        )
        for row, (label, variable, secret) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=5)
            enable_clipboard_paste(ttk.Entry(form, textvariable=variable, show="•" if secret else "", width=42)).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Label(
            form,
            text="После сохранения остальные активные сеансы этой учётной записи будут отозваны.",
            style="Subtle.TLabel",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 4))
        ttk.Button(form, text="Сохранить изменения", command=self.save).grid(row=5, column=1, sticky="e", pady=(8, 0))
        form.columnconfigure(1, weight=1)

    def on_show(self) -> None:
        self.username.set(str(self.panel.state.user.get("username", "")))

    def save(self) -> None:
        if self.new_password.get() != self.confirm_password.get():
            messagebox.showerror("Учётная запись", "Новые пароли не совпадают.")
            return
        payload: dict[str, Any] = {
            "username": self.username.get().strip(),
            "current_password": self.current_password.get(),
        }
        if self.new_password.get():
            payload["new_password"] = self.new_password.get()

        def success(result: dict[str, Any]) -> None:
            token = result.get("token")
            user = result.get("user")
            if not isinstance(token, str) or not isinstance(user, dict):
                messagebox.showerror("Учётная запись", "Сервис вернул неполный ответ.")
                return
            self.panel.api.token = token
            self.panel.update_identity(user)
            self.current_password.set("")
            self.new_password.set("")
            self.confirm_password.set("")
            self.panel.status("Логин и пароль сохранены")

        self.panel.run_async(
            lambda: self.panel.api.request("PATCH", "/v2/me", payload),
            success,
            lambda error: messagebox.showerror("Учётная запись", str(error)),
        )
