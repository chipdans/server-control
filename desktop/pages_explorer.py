"""Mouse-first server explorer with lazy folders, clipboard and atomic transfers."""
from __future__ import annotations

import datetime as dt
import os
import queue
import re
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from direct_files import DirectFilesClient, TransferCancelled
from pages_base import BasePage
from widgets import display_bytes, enable_clipboard_paste

SERVER_ROOT = '/opt/minecraft'


def child_path(parent: str, name: str) -> str:
    if not name or name in ('.', '..') or any(c in name for c in '/\\\x00\r\n'):
        raise ValueError('Введите имя без /, \\ и переносов строк.')
    return str(PurePosixPath(parent) / name)


def windows_clipboard_files() -> list[Path]:
    if os.name != 'nt': return []
    import ctypes
    from ctypes import wintypes
    user = ctypes.windll.user32
    shell = ctypes.windll.shell32
    user.GetClipboardData.argtypes = [wintypes.UINT]
    user.GetClipboardData.restype = wintypes.HANDLE
    shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell.DragQueryFileW.restype = wintypes.UINT
    if not user.OpenClipboard(None): return []
    try:
        drop = user.GetClipboardData(15)  # CF_HDROP, Unicode Explorer file selection
        if not drop: return []
        result = []
        for index in range(shell.DragQueryFileW(drop, 0xFFFFFFFF, None, 0)):
            length = shell.DragQueryFileW(drop, index, None, 0)
            value = ctypes.create_unicode_buffer(length + 1)
            shell.DragQueryFileW(drop, index, value, length + 1)
            result.append(Path(value.value))
        return result
    finally:
        user.CloseClipboard()


class ExplorerPage(BasePage):
    page_id = 'files'
    title = 'Проводник'

    def __init__(self, parent: tk.Misc, panel: Any) -> None:
        super().__init__(parent, panel)
        self.client = DirectFilesClient()
        self.path = ''
        self.busy = False
        self.closed = False
        self.loaded = False
        self.page = self.pages = 1
        self.back: list[str] = []
        self.forward: list[str] = []
        self.entries: dict[str, dict[str, Any]] = {}
        self.clipboard: tuple[str, list[dict[str, Any]]] | None = None
        self.undo: list[str] = []
        self.cancel = threading.Event()
        self.progress_queue: queue.SimpleQueue[tuple[int, int, str]] = queue.SimpleQueue()
        self.address = tk.StringVar(value=SERVER_ROOT)
        self.search = tk.StringVar()
        self.hidden = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value='Откройте папку сборки двойным щелчком.')
        self.page_text = tk.StringVar(value='')
        self.sort = 'name'
        self.reverse = False
        self.buttons: list[tuple[ttk.Button, bool]] = []
        self.editors: list[tk.Toplevel] = []
        self._drag = None

        nav = ttk.Frame(self)
        nav.pack(fill='x', pady=(5, 7))
        for label, action in [('←', self.go_back), ('→', self.go_forward), ('↑', self.go_up), ('⌂', lambda: self.open_directory(''))]:
            ttk.Button(nav, text=label, width=3, command=action).pack(side='left', padx=(0, 4))
        entry = enable_clipboard_paste(ttk.Entry(nav, textvariable=self.address))
        entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Return>', lambda e: self.open_directory(self.address.get()))
        ttk.Button(nav, text='Обновить', command=self.refresh).pack(side='left', padx=5)
        search = enable_clipboard_paste(ttk.Entry(nav, textvariable=self.search, width=22))
        search.pack(side='left')
        search.bind('<Return>', lambda e: self.refresh(reset_page=True))
        ttk.Button(nav, text='Найти', command=lambda: self.refresh(reset_page=True)).pack(side='left', padx=4)
        self.crumbs = ttk.Frame(self)
        self.crumbs.pack(fill='x', pady=(0, 6))

        for actions in [
            [('Загрузить с ПК', self.upload_files, True), ('Загрузить папку', self.upload_folder, True), ('Скачать', self.download, False), ('Новая папка', self.new_folder, True), ('Новый файл', self.new_file, True)],
            [('Копировать', lambda: self.copy('copy'), False), ('Вырезать', lambda: self.copy('move'), True), ('Вставить', self.paste, True), ('Переименовать', self.rename, True), ('Удалить', self.delete, True), ('Отменить удаление', self.restore, True)],
        ]:
            bar = ttk.Frame(self)
            bar.pack(fill='x', pady=(0, 5))
            for label, action, write in actions:
                button = ttk.Button(bar, text=label, command=action)
                button.pack(side='left', padx=(0, 4))
                self.buttons.append((button, write))
        ttk.Checkbutton(bar, text='Скрытые', variable=self.hidden, command=lambda: self.refresh(reset_page=True)).pack(side='right')

        split = ttk.Panedwindow(self, orient='horizontal')
        split.pack(fill='both', expand=True, pady=(4, 0))
        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(right, weight=5)
        self.folders = ttk.Treeview(left, show='tree', selectmode='browse')
        self.folders.column('#0', width=185, minwidth=130)
        ys = ttk.Scrollbar(left, orient='vertical', command=self.folders.yview)
        self.folders.configure(yscrollcommand=ys.set)
        self.folders.pack(side='left', fill='both', expand=True)
        ys.pack(side='right', fill='y')
        self.folders.insert('', 'end', iid='root', text='▣  Все сборки', values=('',), open=True)
        self.folders.bind('<<TreeviewOpen>>', self.expand_folder)
        self.folders.bind('<Double-Button-1>', self.folder_click)
        self.tree = ttk.Treeview(right, columns=('size', 'type', 'modified'), show='tree headings', selectmode='extended')
        self.tree.heading('#0', text='Имя', command=lambda: self.sort_by('name'))
        self.tree.column('#0', width=360, minwidth=180)
        for key, title, width in [('size', 'Размер', 95), ('type', 'Тип', 115), ('modified', 'Изменён', 145)]:
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, minwidth=65, stretch=False)
        ys = ttk.Scrollbar(right, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(right, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.tree.bind('<Double-Button-1>', self.open_selected)
        self.tree.bind('<Return>', self.open_selected)
        self.tree.bind('<Button-3>', self.context_menu)
        self.tree.bind('<ButtonPress-1>', self.drag_start, add='+')
        self.tree.bind('<ButtonRelease-1>', self.drag_end, add='+')
        for key, action in [('<Control-c>', lambda: self.copy('copy')), ('<Control-x>', lambda: self.copy('move')), ('<Control-v>', self.paste), ('<Delete>', self.delete), ('<F2>', self.rename), ('<F5>', self.refresh), ('<BackSpace>', self.go_up), ('<Alt-Left>', self.go_back), ('<Alt-Right>', self.go_forward), ('<Control-z>', self.restore), ('<Control-Shift-N>', self.new_folder), ('<Control-a>', lambda: self.tree.selection_set(self.tree.get_children()))]:
            self.tree.bind(key, lambda e, fn=action: self.hotkey(fn))
        try:
            from tkinterdnd2 import DND_FILES
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind('<<Drop>>', self.drop_files)
        except (ImportError, AttributeError, tk.TclError):
            pass  # File chooser and Explorer Ctrl+V remain available.
        footer = ttk.Frame(self)
        footer.pack(fill='x', pady=6)
        ttk.Button(footer, text='←', width=3, command=lambda: self.open_directory(self.path, self.page-1, False)).pack(side='left')
        ttk.Label(footer, textvariable=self.page_text).pack(side='left', padx=8)
        ttk.Button(footer, text='→', width=3, command=lambda: self.open_directory(self.path, self.page+1, False)).pack(side='left')
        self.progress = ttk.Progressbar(footer, length=150)
        self.progress.pack(side='right')
        self.cancel_button = ttk.Button(footer, text='Отмена передачи', command=self.cancel.set, state='disabled')
        self.cancel_button.pack(side='right', padx=8)
        ttk.Label(self, textvariable=self.status_text, style='Subtle.TLabel', wraplength=1000).pack(fill='x')
        self._progress_after = self.after(120, self.drain_progress)
        self.set_busy(False)

    def hotkey(self, fn):
        fn()
        return 'break'

    def credentials(self): return self.panel.api.terminal_credentials('linux')

    def writable(self): return self.panel.state.has_permission('minecraft.files.write')

    def set_busy(self, value):
        self.busy = value
        for button, write in self.buttons:
            button.configure(state='disabled' if value or (write and not self.writable()) else 'normal')

    def run(self, work, success=None, *, write=False, transfer=False):
        if self.closed or self.busy: return
        if not self.panel.state.has_permission('terminal.linux') or not self.panel.state.has_permission('minecraft.files.read'):
            messagebox.showerror('Проводник', 'Нет разрешения на чтение файлов через SSH.', parent=self)
            return
        if write and not self.writable():
            messagebox.showerror('Проводник', 'Нет разрешения на изменение файлов.', parent=self)
            return
        self.set_busy(True)
        self.cancel.clear()
        self.progress.configure(mode='indeterminate')
        self.progress.start(15)
        self.cancel_button.configure(state='normal' if transfer else 'disabled')
        def finish():
            if self.closed: return
            self.progress.stop()
            self.progress.configure(mode='determinate', value=0)
            self.cancel_button.configure(state='disabled')
            self.set_busy(False)
        def done(value):
            finish()
            if self.closed: return
            if success: success(value)
            else: self.refresh()
        def failed(error):
            finish()
            if self.closed: return
            self.status_text.set(str(error))
            if not isinstance(error, TransferCancelled): messagebox.showerror('Проводник', str(error), parent=self)
            self.refresh()
        self.panel.run_async(work, done, failed)

    def request(self, action, **payload): return self.client.request(self.credentials, dict(action=action, **payload))

    def on_show(self):
        if not self.loaded: self.open_directory('')

    def open_path(self, path): self.open_directory(path)

    def open_directory(self, path, page=1, history=True):
        if self.busy or self.closed: return
        path = str(path)
        if path == SERVER_ROOT: path = ''
        elif path.startswith(SERVER_ROOT + '/'): path = path[len(SERVER_ROOT)+1:]
        if path != self.path:
            self.search.set('')
            page = 1
        payload = dict(path=path, page=max(1, page), search=self.search.get(), hidden=self.hidden.get(), sort=self.sort, reverse=self.reverse)
        def work():
            result = self.request('list', **payload)
            roots = self.request('list', path='', directories_only=True, page_size=1000, hidden=False) if not self.loaded or not path else None
            return result, roots
        def done(value):
            result, roots = value
            if history and self.path != result['path']:
                self.back.append(self.path)
                self.back = self.back[-100:]
                self.forward.clear()
            self.path = result['path']
            self.page, self.pages = result['page'], result['pages']
            self.loaded = True
            self.address.set(SERVER_ROOT + ('/' + self.path if self.path else ''))
            self.tree.delete(*self.tree.get_children())
            self.entries.clear()
            for i, entry in enumerate(result['entries']):
                iid = str(i)
                self.entries[iid] = entry
                directory = entry['type'] == 'directory'
                kind = {'directory':'Папка', 'link':'Ссылка', 'special':'Спец. файл'}.get(entry['type'], (PurePosixPath(entry['name']).suffix.upper().lstrip('.') + ' файл').strip())
                self.tree.insert('', 'end', iid=iid, text=('▣  ' if directory else '   ') + entry['name'], values=('—' if directory else display_bytes(entry['size']), kind, dt.datetime.fromtimestamp(entry['modified']).strftime('%d.%m.%Y %H:%M')))
            self.page_text.set(f"{result['total']} элементов · {self.page}/{self.pages}")
            self.status_text.set('Двойной щелчок — открыть. Ctrl+C / Ctrl+X / Ctrl+V — копировать, вырезать, вставить. Поиск — по текущей папке.')
            self.render_crumbs()
            if roots: self.fill_folders('root', roots['entries'])
        self.run(work, done)

    def render_crumbs(self):
        for w in self.crumbs.winfo_children(): w.destroy()
        ttk.Button(self.crumbs, text='Все сборки', command=lambda: self.open_directory('')).pack(side='left')
        parts = PurePosixPath(self.path).parts
        # Only visible trailing segments are needed; address always contains the full path.
        for i, name in list(enumerate(parts))[-5:]:
            value = '/'.join(parts[:i+1])
            ttk.Label(self.crumbs, text=' › ').pack(side='left')
            ttk.Button(self.crumbs, text=name[:32], command=lambda p=value: self.open_directory(p)).pack(side='left')

    def fill_folders(self, parent, entries):
        self.folders.delete(*self.folders.get_children(parent))
        for item in entries:
            node = self.folders.insert(parent, 'end', text='▣  ' + item['name'], values=(item['path'],))
            self.folders.insert(node, 'end', text='…', tags=('placeholder',))

    def expand_folder(self, event=None):
        node = self.folders.focus()
        if not node or self.busy: return
        values = self.folders.item(node, 'values')
        if not values: return
        self.run(lambda: self.request('list', path=values[0], directories_only=True, page_size=1000), lambda r: self.fill_folders(node, r['entries']))

    def folder_click(self, event=None):
        node = self.folders.identify_row(event.y) if event else self.folders.focus()
        values = self.folders.item(node, 'values') if node else ()
        if values: self.open_directory(values[0])

    def refresh(self, reset_page=False): self.open_directory(self.path, 1 if reset_page else self.page, False)
    def go_up(self):
        parent = str(PurePosixPath(self.path).parent)
        self.open_directory('' if parent == '.' else parent)
    def go_back(self):
        if self.back and not self.busy:
            dest = self.back.pop()
            self.forward.append(self.path)
            self.open_directory(dest, history=False)
    def go_forward(self):
        if self.forward and not self.busy:
            dest = self.forward.pop()
            self.back.append(self.path)
            self.open_directory(dest, history=False)
    def sort_by(self, key):
        self.reverse = not self.reverse if self.sort == key else False
        self.sort = key
        self.refresh()

    def selected(self): return [dict(self.entries[i]) for i in self.tree.selection() if i in self.entries]

    def open_selected(self, event=None):
        selected = self.selected()
        if not selected or self.busy: return
        item = selected[0]
        if item['type'] == 'directory': self.open_directory(item['path'])
        elif item['type'] == 'file': self.run(lambda: self.request('read', path=item['path']), lambda data: self.editor(item, data))
        else: messagebox.showinfo('Проводник', 'Ссылки и специальные файлы доступны только для просмотра сведений.', parent=self)

    def name(self, title, initial=''):
        value = simpledialog.askstring(title, 'Имя:', initialvalue=initial, parent=self)
        if value is None: return None
        try: child_path('', value)
        except ValueError as error:
            messagebox.showerror(title, str(error), parent=self)
            return None
        return value

    def new_folder(self):
        if self.busy: return
        name = self.name('Новая папка')
        if name:
            path = child_path(self.path, name)
            self.run(lambda: self.request('mkdir', path=path), write=True)

    def new_file(self):
        if self.busy: return
        name = self.name('Новый текстовый файл', 'Новый файл.txt')
        if name:
            path = child_path(self.path, name)
            self.run(lambda: self.request('write', path=path, content='', expected=None), write=True)

    def rename(self):
        items = self.selected()
        if self.busy or len(items) != 1: return
        item = items[0]
        name = self.name('Переименовать', item['name'])
        if name and name != item['name']:
            dest = child_path(self.path, name)
            self.run(lambda: self.request('move', path=item['path'], destination=dest, source_version=item['version'], expected=None), write=True)

    def copy(self, mode):
        selected = self.selected()
        if selected and not self.busy:
            self.clipboard = mode, selected
            # Mark our clipboard so external Explorer copies can supersede it later.
            self.clipboard_clear()
            self.clipboard_append('server-control-files:' + mode)
            self.status_text.set(f"{'Вырезано' if mode == 'move' else 'Скопировано'}: {len(selected)}. Откройте папку и нажмите «Вставить».")

    def paste(self):
        if self.busy: return
        local = windows_clipboard_files()
        if local:
            self.upload_paths(local)
            return
        if not self.clipboard: return
        mode, items = self.clipboard
        self.paste_remote(mode, items, self.path)

    def paste_remote(self, mode, items, target):
        plans = []
        for item in items:
            name = item['name']
            if child_path(target, name) == item['path']:
                if mode == 'move': continue
                name = self.name('Имя копии', 'Копия — ' + name)
                if not name: continue
            plans.append((item, child_path(target, name)))
        if not plans: return
        def work():
            for item, dest in plans:
                self.request(mode, path=item['path'], destination=dest, source_version=item['version'], expected=None)
        def done(_):
            if mode == 'move': self.clipboard = None
            self.refresh()
        self.run(work, done, write=True)

    def delete(self):
        items = self.selected()
        if not items or self.busy: return
        names = '\n'.join(i['name'] for i in items[:8])
        if not messagebox.askyesno('Переместить в корзину?', f'Выбрано: {len(items)}\n{names}\n\nФайлы исчезнут из сборки. Удаление можно отменить кнопкой или Ctrl+Z. Для изменения файлов работающего мира сначала остановите сборку.', parent=self): return
        def work():
            for item in items:
                token = self.request('delete', path=item['path'], expected=item['version'])['token']
                self.panel.post_ui(lambda t=token: self.undo.append(t))
        self.run(work, write=True)

    def restore(self):
        if self.busy or not self.undo: return
        token = self.undo[-1]
        def done(_):
            self.undo.pop()
            self.refresh()
        self.run(lambda: self.request('restore', token=token), done, write=True)

    def confirm_from_worker(self, title, text):
        ready = threading.Event()
        answer = [False]
        def ask():
            try:
                if not self.closed and not self.cancel.is_set(): answer[0] = messagebox.askyesno(title, text, parent=self)
            finally: ready.set()
        self.panel.post_ui(ask)
        while not ready.wait(.1):
            if self.closed or self.cancel.is_set(): raise TransferCancelled('Передача отменена')
        return answer[0]

    def progress_callback(self, label):
        last = [0.0]
        def update(current, total):
            now = time.monotonic()
            if now-last[0] > .12 or current == total:
                self.progress_queue.put((current, total, label))
                last[0] = now
        return update

    def drain_progress(self):
        if self.closed: return
        last = None
        while True:
            try: last = self.progress_queue.get_nowait()
            except queue.Empty: break
        if last and self.busy:
            current, total, name = last
            self.progress.stop()
            self.progress.configure(mode='determinate', value=current*100/total if total else 100)
            self.status_text.set(f'{name}: {display_bytes(current)} / {display_bytes(total)}')
        self._progress_after = self.after(120, self.drain_progress)

    def upload_files(self):
        if self.busy: return
        files = filedialog.askopenfilenames(parent=self, title='Загрузить файлы на сервер')
        if files: self.upload_paths([Path(f) for f in files])

    def upload_folder(self):
        if self.busy: return
        folder = filedialog.askdirectory(parent=self, title='Загрузить папку вместе с содержимым')
        if folder: self.upload_paths([Path(folder)])

    def upload_paths(self, paths, target=None):
        if self.busy: return
        target = self.path if target is None else target
        def work():
            count = 0
            skipped = 0
            def upload_file(source, dest):
                nonlocal count, skipped
                if self.cancel.is_set(): raise TransferCancelled('Передача отменена. Уже завершённые файлы сохранены.')
                info = self.request('stat', path=dest)
                if info['directory']: raise ValueError('Вместо файла существует папка: ' + dest)
                if info['version'] is not None and not self.confirm_from_worker('Заменить файл?', dest + '\n\nФайл существует. Заменить его?'):
                    skipped += 1
                    return
                self.client.upload(self.credentials, source, dest, info['version'], self.progress_callback(source.name), self.cancel.is_set)
                count += 1
            for source in paths:
                if source.is_symlink(): raise ValueError('Загрузка ссылок не поддерживается: ' + source.name)
                dest = child_path(target, source.name)
                if source.is_dir():
                    for base, dirs, files in os.walk(source, followlinks=False):
                        if self.cancel.is_set(): raise TransferCancelled('Передача отменена')
                        for name in dirs + files:
                            if (Path(base)/name).is_symlink(): raise ValueError('В папке есть символическая ссылка: ' + name)
                        relative = Path(base).relative_to(source)
                        remote = str(PurePosixPath(dest) / PurePosixPath(relative.as_posix()))
                        self.request('ensure_directory', path=remote)
                        for name in files: upload_file(Path(base)/name, child_path(remote, name))
                else: upload_file(source, dest)
            return count, skipped
        def done(counts):
            self.panel.status(f'Загружено файлов: {counts[0]}; пропущено: {counts[1]}')
            self.refresh()
        self.run(work, done, write=True, transfer=True)

    def download(self):
        items = self.selected()
        if not items or self.busy: return
        if len(items) == 1:
            item = items[0]
            name = item['name'] + ('.zip' if item['type'] == 'directory' else '')
            value = filedialog.asksaveasfilename(parent=self, title='Скачать с сервера', initialfile=name)
            if not value: return
            plan = [(item, Path(value))]
        else:
            value = filedialog.askdirectory(parent=self, title='Сохранить выбранные файлы')
            if not value: return
            plan = []
            for item in items:
                name = item['name'] + ('.zip' if item['type'] == 'directory' else '')
                if os.name == 'nt' and (re.search(r'[<>:"/\\|?*]', name) or name.endswith(('.', ' ')) or name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}):
                    messagebox.showerror('Скачать', 'Имя несовместимо с Windows. Скачайте отдельно с другим именем: ' + name, parent=self)
                    return
                target = Path(value) / name
                if target.exists() and not messagebox.askyesno('Заменить?', str(target), parent=self): continue
                plan.append((item, target))
        def work():
            for item, target in plan:
                if self.cancel.is_set(): raise TransferCancelled('Скачивание отменено')
                self.client.download(self.credentials, item['path'], target, self.progress_callback(item['name']), self.cancel.is_set)
        self.run(work, lambda _: self.status_text.set('Скачивание завершено.'), transfer=True)

    def editor(self, item, data):
        win = tk.Toplevel(self)
        self.editors.append(win)
        win.title(item['name'] + ' — редактор')
        win.geometry('900x620')
        text = tk.Text(win, wrap='none', undo=True, bg='#0b1824', fg='#e5edf8', insertbackground='white', font=('Consolas', 11))
        text.pack(fill='both', expand=True)
        text.insert('1.0', data['content'])
        text.edit_modified(False)
        if not self.writable(): text.configure(state='disabled')
        def close():
            if text.edit_modified() and not messagebox.askyesno('Закрыть редактор?', 'Несохранённые изменения будут потеряны.', parent=win): return
            self.editors.remove(win)
            win.destroy()
        def save():
            if self.busy or not self.writable(): return
            content = text.get('1.0', 'end-1c')
            def work():
                fd, name = tempfile.mkstemp()
                try:
                    with os.fdopen(fd, 'wb') as f: f.write(content.encode('utf-8-sig' if data.get('bom') else 'utf-8'))
                    return self.client.upload(self.credentials, Path(name), item['path'], data['version'], self.progress_callback(item['name']), self.cancel.is_set)
                finally: Path(name).unlink(missing_ok=True)
            def done(result):
                data['version'] = result['version']
                if win.winfo_exists():
                    if text.get('1.0','end-1c') == content: text.edit_modified(False)
                    win.title(item['name'] + ' — сохранено')
            self.run(work, done, write=True, transfer=True)
        bar = ttk.Frame(win, padding=8)
        bar.pack(fill='x')
        ttk.Button(bar, text='Сохранить · Ctrl+S', command=save, state='normal' if self.writable() else 'disabled').pack(side='left')
        ttk.Label(bar, text=SERVER_ROOT + '/' + item['path']).pack(side='left', padx=10)
        win.bind('<Control-s>', lambda e: self.hotkey(save))
        win.protocol('WM_DELETE_WINDOW', close)

    def context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection(): self.tree.selection_set(row)
        if not row: self.tree.selection_remove(*self.tree.selection())
        menu = tk.Menu(self, tearoff=False)
        for label, fn, write in [('Открыть', self.open_selected, False), ('Скачать', self.download, False), ('Свойства', self.properties, False), ('Копировать  Ctrl+C', lambda: self.copy('copy'), False), ('Вырезать  Ctrl+X', lambda: self.copy('move'), True), ('Вставить  Ctrl+V', self.paste, True), ('Переименовать  F2', self.rename, True), ('Удалить  Del', self.delete, True), ('Новая папка', self.new_folder, True)]:
            menu.add_command(label=label, command=fn, state='disabled' if self.busy or (write and not self.writable()) else 'normal')
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def properties(self):
        selected = self.selected()
        if not selected: return
        item = selected[0]
        messagebox.showinfo('Свойства', f"{item['name']}\n\nПуть: {SERVER_ROOT}/{item['path']}\nТип: {item['type']}\nРазмер файла: {display_bytes(item['size']) if item['type']=='file' else '—'}\nПрава: {item['mode']}\nИзменён: {dt.datetime.fromtimestamp(item['modified']):%d.%m.%Y %H:%M:%S}", parent=self)

    def drag_start(self, event):
        row = self.tree.identify_row(event.y)
        self._drag = (event.x, event.y, row) if row else None

    def drag_end(self, event):
        start, self._drag = self._drag, None
        if not start or self.busy or abs(event.x-start[0]) + abs(event.y-start[1]) < 12: return
        row = self.tree.identify_row(event.y)
        item = self.entries.get(row)
        if not item or item['type'] != 'directory' or row == start[2]: return
        items = self.selected()
        if any(x['path'] == item['path'] for x in items): return
        mode = 'copy' if event.state & 0x4 else 'move'
        if messagebox.askyesno('Перенести файлы?', f"{'Скопировать' if mode=='copy' else 'Переместить'} {len(items)} элементов в {item['name']}?", parent=self): self.paste_remote(mode, items, item['path'])

    def drop_files(self, event):
        if self.busy or not self.writable(): return 'refuse_drop'
        files = [Path(p) for p in self.tk.splitlist(event.data)]
        row = self.tree.identify_row(event.y_root-self.tree.winfo_rooty())
        item = self.entries.get(row)
        target = item['path'] if item and item['type'] == 'directory' else self.path
        self.after_idle(lambda: self.upload_paths(files, target))
        return 'copy'

    def close(self):
        if self.closed: return
        self.closed = True
        self.cancel.set()
        self.after_cancel(self._progress_after)
        self.client.abort()
        for editor in self.editors:
            if editor.winfo_exists(): editor.destroy()
