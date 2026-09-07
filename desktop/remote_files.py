"""Self-contained, fixed-root file operations executed on Debian over SSH."""

REMOTE_FILE_PROGRAM = r'''
import base64, hashlib, json, os, shutil, stat, sys, tempfile, time, uuid, zipfile
from pathlib import Path, PurePosixPath

ROOT = Path('/opt/minecraft')
TRASH = '.server-control-trash'
TEMP_PREFIX = '.sc-transfer-'
CHUNK = 256 * 1024

def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)

def safe(value, root_ok=True):
    value = str(value or '')
    if '\x00' in value or '\\' in value:
        raise ValueError('Некорректный путь')
    if value == str(ROOT): value = ''
    elif value.startswith(str(ROOT) + '/'): value = value[len(str(ROOT)) + 1:]
    p = PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or any(x == TRASH or x.startswith(TEMP_PREFIX) for x in p.parts):
        raise ValueError('Доступ разрешён только внутри /opt/minecraft')
    result = ROOT
    if ROOT.is_symlink(): raise ValueError('Корень сборок не должен быть ссылкой')
    for part in p.parts:
        result = result / part
        if result.is_symlink(): raise ValueError('Символические ссылки недоступны')
    if result == ROOT and not root_ok: raise ValueError('Нельзя изменять корневую папку сборок')
    return result

def relative(p): return p.relative_to(ROOT).as_posix() if p != ROOT else ''

def version(p):
    try:
        s = p.lstat()
        return [s.st_mtime_ns, s.st_size, s.st_ino]
    except FileNotFoundError: return None

def check_target(p, expected):
    if version(p) != expected:
        raise FileExistsError('Файл уже существует или изменён. Обновите папку и повторите действие: ' + p.name)

def inherit(p, parent):
    s = parent.stat()
    if hasattr(os, 'chown') and os.geteuid() == 0: os.chown(p, s.st_uid, s.st_gid)
    p.chmod(0o775 if p.is_dir() else 0o664)

def listing(req):
    folder = safe(req.get('path'))
    items = []
    needle = str(req.get('search', '')).casefold()
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name == TRASH or entry.name.startswith(TEMP_PREFIX): continue
            if needle not in entry.name.casefold(): continue
            if not req.get('hidden', False) and entry.name.startswith('.'): continue
            try: s = entry.stat(follow_symlinks=False)
            except FileNotFoundError: continue
            kind = 'link' if stat.S_ISLNK(s.st_mode) else 'directory' if stat.S_ISDIR(s.st_mode) else 'file' if stat.S_ISREG(s.st_mode) else 'special'
            if req.get('directories_only') and kind != 'directory': continue
            items.append(dict(name=entry.name, path=relative(Path(entry.path)), type=kind,
                              size=s.st_size, modified=s.st_mtime, mode=stat.filemode(s.st_mode),
                              version=[s.st_mtime_ns, s.st_size, s.st_ino]))
            if len(items) > 100000: raise ValueError('Слишком большой каталог. Уточните поиск по имени.')
    sort = req.get('sort', 'name')
    items.sort(key=lambda e: e.get(sort, e['name']).casefold() if isinstance(e.get(sort, e['name']), str) else e[sort], reverse=bool(req.get('reverse')))
    items.sort(key=lambda e: e['type'] != 'directory')
    size = min(1000, max(1, int(req.get('page_size', 200))))
    pages = max(1, (len(items) + size - 1) // size)
    page = min(pages, max(1, int(req.get('page', 1))))
    return dict(path=relative(folder), entries=items[(page-1)*size:page*size], page=page, pages=pages, total=len(items))

def tree_files(path):
    if path.is_file():
        yield path
        return
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            child = Path(base) / name
            if child.is_symlink() or not (child.is_dir() or child.is_file()):
                raise ValueError('В папке есть ссылка или специальный файл: ' + relative(child))
        for name in files: yield Path(base) / name

def move_to_trash(path):
    trash = ROOT / TRASH
    if trash.is_symlink(): raise ValueError('Некорректная корзина')
    trash.mkdir(mode=0o700, exist_ok=True)
    token = uuid.uuid4().hex
    target = trash / token
    target.mkdir(mode=0o700)
    (target / 'record.json').write_text(json.dumps({'path':relative(path), 'time':time.time()}), encoding='utf-8')
    try: shutil.move(str(path), str(target / 'item'))
    except Exception:
        shutil.rmtree(target)
        raise
    return token

def dispatch(req):
    action = req['action']
    if action == 'list': return listing(req)
    if action == 'stat':
        p = safe(req['path'])
        return dict(path=relative(p), version=version(p), directory=p.is_dir())
    if action == 'restore':
        token = str(req.get('token', ''))
        if len(token) != 32 or any(x not in '0123456789abcdef' for x in token): raise ValueError('Некорректная запись корзины')
        trash = ROOT / TRASH
        if trash.is_symlink(): raise ValueError('Некорректная корзина')
        record = trash / token
        if record.is_symlink() or (record / 'record.json').is_symlink() or (record / 'item').is_symlink(): raise ValueError('Некорректная запись корзины')
        info = json.loads((record / 'record.json').read_text())
        p = safe(info['path'], False)
        check_target(p, None)
        shutil.move(str(record / 'item'), str(p))
        shutil.rmtree(record)
        return dict(path=relative(p))
    p = safe(req['path'], False)
    if action == 'mkdir':
        p.mkdir()
        inherit(p, p.parent)
    elif action == 'ensure_directory':
        if not p.exists():
            p.mkdir()
            inherit(p, p.parent)
        elif not p.is_dir(): raise FileExistsError('Вместо папки существует файл: ' + p.name)
    elif action == 'delete':
        check_target(p, req['expected'])
        return dict(token=move_to_trash(p))
    elif action in ('copy', 'move'):
        check_target(p, req['source_version'])
        target = safe(req['destination'], False)
        if target == p or p in target.parents: raise ValueError('Нельзя поместить папку внутрь самой себя')
        check_target(target, req.get('expected'))
        if target.is_dir(): raise FileExistsError('Папка уже существует. Выберите другое имя.')
        if action == 'move':
            if target.exists(): raise FileExistsError('Файл назначения уже существует. Выберите другое имя.')
            shutil.move(str(p), str(target))
        else:
            temp = target.parent / (TEMP_PREFIX + uuid.uuid4().hex)
            try:
                if p.is_dir():
                    for unused in tree_files(p): pass
                    shutil.copytree(p, temp)
                elif p.is_file(): shutil.copy2(p, temp)
                else: raise ValueError('Недоступный тип файла')
                check_target(target, req.get('expected'))
                if temp.is_dir():
                    for base, dirs, files in os.walk(temp):
                        inherit(Path(base), target.parent)
                        for name in files: inherit(Path(base) / name, target.parent)
                else: inherit(temp, target.parent)
                os.replace(temp, target)
            finally:
                if temp.is_dir(): shutil.rmtree(temp)
                elif temp.exists(): temp.unlink()
        return dict(path=relative(target))
    elif action == 'read':
        if not p.is_file() or p.stat().st_size > 2*1024*1024: raise ValueError('Редактор открывает текстовые файлы до 2 МБ. Используйте «Скачать».')
        before = version(p)
        content = p.read_bytes()
        if b'\x00' in content: raise ValueError('Это двоичный файл. Используйте «Скачать».')
        text = content.decode('utf-8-sig')
        if version(p) != before: raise ValueError('Файл изменился во время чтения. Откройте его снова.')
        return dict(content=text, version=before, bom=content.startswith(b'\xef\xbb\xbf'))
    elif action in ('write', 'upload'):
        check_target(p, req.get('expected'))
        if p.exists() and not p.is_file(): raise ValueError('Нельзя заменить папку файлом')
        previous = p.stat() if p.exists() else None
        fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=p.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, 'wb') as out:
                if action == 'write':
                    data = str(req.get('content', '')).encode('utf-8-sig' if req.get('bom') else 'utf-8')
                    if len(data) > 2*1024*1024: raise ValueError('Файл слишком велик для редактора')
                    out.write(data)
                else:
                    left = int(req['size'])
                    if left < 0: raise ValueError('Некорректный размер')
                    emit({'ready': True})
                    while left:
                        block = sys.stdin.buffer.read(min(CHUNK, left))
                        if not block: raise EOFError('Передача прервана. Исходный файл сохранён.')
                        out.write(block)
                        left -= len(block)
                    if sys.stdin.buffer.read(1) != b'C':
                        raise EOFError('Передача отменена до подтверждения. Исходный файл сохранён.')
                out.flush()
                os.fsync(out.fileno())
            safe(relative(p), False)
            check_target(p, req.get('expected'))
            if previous:
                if hasattr(os, 'chown'): os.chown(tmp, previous.st_uid, previous.st_gid)
                tmp.chmod(stat.S_IMODE(previous.st_mode))
            else: inherit(tmp, p.parent)
            os.replace(tmp, p)
        finally:
            if tmp.exists(): tmp.unlink()
        return dict(version=version(p))
    else: raise ValueError('Неизвестная файловая операция')
    return dict(path=relative(p))

def download(req):
    p = safe(req['path'], False)
    archive = None
    try:
        if p.is_dir():
            fd, name = tempfile.mkstemp(prefix='sc-download-', suffix='.zip')
            os.close(fd)
            archive = Path(name)
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                for f in tree_files(p): z.write(f, str(Path(p.name) / f.relative_to(p)))
                for base, dirs, files in os.walk(p):
                    if not dirs and not files: z.write(base, str(Path(p.name) / Path(base).relative_to(p)) + '/')
            source = archive
        else: source = p
        if not source.is_file(): raise ValueError('Можно скачивать только обычные файлы и папки')
        with source.open('rb') as stream:
            s = os.fstat(stream.fileno())
            emit(dict(ready=True, size=s.st_size))
            left = s.st_size
            while left:
                block = stream.read(min(CHUNK, left))
                if not block: raise EOFError('Файл изменился во время скачивания')
                sys.stdout.buffer.write(block)
                left -= len(block)
            sys.stdout.buffer.flush()
            after = os.fstat(stream.fileno())
            if (s.st_mtime_ns, s.st_size) != (after.st_mtime_ns, after.st_size): raise ValueError('Файл изменился во время скачивания')
    finally:
        if archive is not None: archive.unlink(missing_ok=True)

if __name__ == '__main__':
    try:
        request = json.loads(base64.b64decode(sys.argv[1]))
        if request['action'] == 'download': download(request)
        else: emit({'ok':True, **dispatch(request)})
    except Exception as error:
        emit({'ok':False, 'error':str(error)})
        sys.exit(1)
'''
