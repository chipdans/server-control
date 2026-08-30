"""Build an accurate translation export from the active client resource stack."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Callable


MAX_LANG_FILE_BYTES = 8 * 1024 * 1024
MAX_EXPORT_BYTES = 512 * 1024 * 1024
MAX_TASKS = 250_000
MAX_ARCHIVE_MEMBERS = 250_000
LANG_PATH = re.compile(r"^assets/([^/]+)/lang/(en_us|ru_ru)\.(json|lang)$", re.IGNORECASE)
ENGLISH_GRAMMAR_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "click",
    "do", "does", "for", "from", "get", "has", "have", "if", "in", "into", "is", "it",
    "let", "me", "not", "of", "on", "only", "or", "that", "the", "this", "to", "use",
    "was", "when", "will", "with", "you", "your",
}
VISIBLE_OBJECT_KEY_PREFIXES = (
    "advancement.", "affix.", "biome.", "block.", "effect.", "enchantment.", "entity.",
    "gem_", "gui.", "item.", "message.", "mob.", "perk.", "quest.", "stat.", "tooltip.",
)


def _normalized(value: Any) -> str:
    text = re.sub(r"§.", "", str(value or ""))
    text = re.sub(r"%(?:\d+\$)?[a-zA-Z%]", "", text)
    text = re.sub(r"\{[^{}]*\}", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _looks_english(value: Any) -> bool:
    text = _normalized(value)
    latin_words = re.findall(r"[a-zA-Z]{3,}", text)
    if not latin_words:
        return False
    if re.search(r"[а-яё]", text, re.IGNORECASE):
        return len(latin_words) >= 2 or sum(len(word) for word in latin_words) >= 12
    if not re.search(r"\s", text) and re.fullmatch(r"[a-z0-9_.:/+@#-]+", text, re.IGNORECASE) and re.search(r"[_.:/]", text):
        return False
    return True


def _nontranslatable_value(value: Any, key: Any = "") -> bool:
    text = re.sub(r"§.", "", str(value or "")).strip()
    lowered_key = str(key or "").casefold()
    if not text or lowered_key in {"_comment", "comment", "credits", "author"}:
        return True
    if re.fullmatch(r"[MDCLXVI]+", text):
        return True
    if re.fullmatch(r"[\[(]?(?:SHIFT|CTRL|CONTROL|ALT|ENTER|ESC|ESCAPE|TAB|SPACE|LMB|RMB|MMB|WASD|F\d{1,2})[\])]?", text, re.IGNORECASE):
        return True
    if text.casefold() in {"true", "false", "on", "off", "yes", "no", "default", "none", "auto", "enabled", "disabled"}:
        return True
    if re.fullmatch(r"(?:https?://\S+|[a-z0-9_.-]+:[a-z0-9_./-]+|/[a-z0-9_./#:-]+)", text, re.IGNORECASE):
        return True
    if re.fullmatch(r"[dMyHhmsS:/ ._-]{4,}", text) and re.search(r"[dMyHhmsS]", text):
        return True
    if ("music_disc" in lowered_key or ".sound." in lowered_key or ".sounds." in lowered_key) and re.fullmatch(
        r"[^\n]{1,80}\s[-–—]\s[^\n]{1,80}", text
    ):
        return True
    return False


def _punctuation_equivalent(first: Any, second: Any) -> bool:
    normalize = lambda value: re.sub(r"[^0-9a-zа-яё]+", "", str(value or "").casefold())
    return bool(normalize(first)) and normalize(first) == normalize(second)


def _mixed_english_review(value: Any) -> bool:
    text = re.sub(r"https?://\S+", "", str(value or ""))
    text = re.sub(r"(?:[#/]?[a-z0-9_.-]+:[a-z0-9_./-]+|/[a-z0-9_./#:-]+)", "", text, flags=re.IGNORECASE)
    latin = re.findall(r"[A-Za-z]{2,}", text)
    cyrillic_letters = re.findall(r"[А-Яа-яЁё]", text)
    if not latin or not cyrillic_letters:
        return False
    grammar = sum(word.casefold() in ENGLISH_GRAMMAR_WORDS for word in latin)
    latin_letters = sum(len(word) for word in latin)
    ratio = latin_letters / max(1, latin_letters + len(cyrillic_letters))
    return grammar >= 2 or (grammar >= 1 and ratio >= 0.35) or (len(latin) >= 3 and ratio >= 0.45)


def _ambiguous_short_name(value: Any, key: Any) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", str(value or ""))
    if not 1 <= len(words) <= 4 or str(key or "").casefold().startswith(VISIBLE_OBJECT_KEY_PREFIXES):
        return False
    return all(word[:1].isupper() or word.isupper() for word in words)


def _translation_decision(source: Any, current: Any | None, key: Any) -> tuple[str, str]:
    if _nontranslatable_value(source, key):
        return "", ""
    if current is None:
        return ("needs_translation", "missing") if _looks_english(source) else ("", "")
    source_normalized = _normalized(source)
    current_normalized = _normalized(current)
    if source_normalized and source_normalized == current_normalized and _looks_english(source):
        if _ambiguous_short_name(source, key):
            return "review_required", "ambiguous_name"
        return "needs_translation", "identical_to_english"
    if _punctuation_equivalent(source, current) or _nontranslatable_value(current, key):
        return "", ""
    if re.search(r"[а-яё]", str(current), re.IGNORECASE):
        return ("review_required", "mixed_language") if _mixed_english_review(current) else ("", "")
    if _looks_english(current):
        if _ambiguous_short_name(current, key):
            return "review_required", "ambiguous_name"
        return "needs_translation", "contains_english"
    return "", ""


def _parse_language(payload: bytes, suffix: str) -> dict[str, str]:
    text = payload.decode("utf-8-sig", "replace")
    if suffix.casefold() == ".json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items() if isinstance(item, (str, int, float, bool))}
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!", "//")):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            key, separator, value = line.partition(":")
        if separator and key.strip():
            values[key.strip().strip("\"'")] = value.strip().strip("\"'")
    return values


class _Catalog:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, dict[str, str]]] = {"en_us": {}, "ru_ru": {}}
        self.origins: dict[str, dict[str, dict[str, str]]] = {"en_us": {}, "ru_ru": {}}
        self.sources: list[str] = []
        self.errors: list[str] = []
        self.lang_files = 0

    def apply(self, language: str, namespace: str, payload: dict[str, str], source: str) -> None:
        namespace = namespace.casefold()
        values = self.values[language].setdefault(namespace, {})
        origins = self.origins[language].setdefault(namespace, {})
        values.update(payload)
        origins.update({key: source for key in payload})
        self.lang_files += 1

    def remember_source(self, source: str) -> None:
        if source not in self.sources:
            self.sources.append(source)


def _scan_archive(path: Path, label: str, catalog: _Catalog) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                catalog.errors.append(f"{label}: слишком много файлов")
                return
            used = False
            for info in members:
                match = LANG_PATH.fullmatch(info.filename.replace("\\", "/"))
                if not match or info.is_dir() or info.file_size > MAX_LANG_FILE_BYTES:
                    continue
                payload = _parse_language(archive.read(info), Path(info.filename).suffix)
                if payload:
                    catalog.apply(match.group(2).casefold(), match.group(1), payload, f"{label}!/{info.filename}")
                    used = True
            if used:
                catalog.remember_source(label)
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as error:
        catalog.errors.append(f"{label}: {error}")


def _scan_assets_directory(assets: Path, label: str, catalog: _Catalog) -> None:
    if not assets.is_dir() or assets.is_symlink():
        return
    used = False
    try:
        namespaces = sorted((path for path in assets.iterdir() if path.is_dir() and not path.is_symlink()), key=lambda path: path.name.casefold())
    except OSError as error:
        catalog.errors.append(f"{label}: {error}")
        return
    for namespace in namespaces:
        lang = namespace / "lang"
        if not lang.is_dir() or lang.is_symlink():
            continue
        for language in ("en_us", "ru_ru"):
            for suffix in (".json", ".lang"):
                path = lang / (language + suffix)
                try:
                    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_LANG_FILE_BYTES:
                        continue
                    payload = _parse_language(path.read_bytes(), suffix)
                except OSError as error:
                    catalog.errors.append(f"{label}/{path.relative_to(assets).as_posix()}: {error}")
                    continue
                if payload:
                    source = f"{label}/{path.relative_to(assets).as_posix()}"
                    catalog.apply(language, namespace.name, payload, source)
                    used = True
    if used:
        catalog.remember_source(label)


def _scan_resource_source(path: Path, label: str, catalog: _Catalog) -> None:
    if path.is_file() and path.suffix.casefold() in {".zip", ".jar"}:
        _scan_archive(path, label, catalog)
    elif path.is_dir() and not path.is_symlink():
        assets = path / "assets"
        _scan_assets_directory(assets if assets.is_dir() else path, label, catalog)


def _read_enabled_resourcepacks(directory: Path) -> tuple[list[Path], str]:
    resourcepacks = directory / "resourcepacks"
    if not resourcepacks.is_dir():
        return [], "none"
    configured: list[str] | None = None
    options = directory / "options.txt"
    try:
        for line in options.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            key, separator, raw = line.partition(":")
            if separator and key.casefold() in {"resourcepacks", "resource_packs"}:
                value = json.loads(raw)
                if isinstance(value, list):
                    configured = [str(item) for item in value]
                break
    except (OSError, json.JSONDecodeError):
        configured = None
    if configured is not None:
        selected: list[Path] = []
        for value in configured:
            if not value.startswith("file/"):
                continue
            relative = urllib.parse.unquote(value[5:]).replace("\\", "/").lstrip("/")
            candidate = resourcepacks / relative
            try:
                candidate.resolve(strict=False).relative_to(resourcepacks.resolve(strict=False))
            except ValueError:
                continue
            if candidate.exists():
                selected.append(candidate)
        return selected, "enabled_from_options"
    try:
        installed = sorted(
            (path for path in resourcepacks.iterdir() if path.is_dir() or path.suffix.casefold() == ".zip"),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        installed = []
    return installed, "all_installed_options_missing"


def scan_client_languages(directory: str | Path, progress: Callable[[float, str], None] | None = None) -> tuple[_Catalog, dict[str, Any]]:
    root = Path(directory).expanduser().resolve(strict=True)
    if not (root / "mods").is_dir():
        raise ValueError("В выбранной папке нет каталога mods. Выберите корень клиентской сборки Minecraft.")
    catalog = _Catalog()
    jars = sorted((path for path in (root / "mods").glob("*.jar") if path.is_file() and not path.is_symlink()), key=lambda path: path.name.casefold())
    total = max(1, len(jars))
    for index, jar in enumerate(jars, start=1):
        _scan_archive(jar, f"mods/{jar.name}", catalog)
        if progress and (index == total or index % 10 == 0):
            progress(index * 65 / total, f"Проверяю моды клиента: {index}/{total}")

    for relative in ("resources", "kubejs/assets"):
        source = root / relative
        _scan_resource_source(source, relative, catalog)

    for relative in ("openloader/resources", "config/openloader/resources"):
        container = root / relative
        if not container.is_dir():
            continue
        for source in sorted(container.iterdir(), key=lambda path: path.name.casefold()):
            _scan_resource_source(source, f"{relative}/{source.name}", catalog)

    enabled_packs, resourcepack_mode = _read_enabled_resourcepacks(root)
    for index, source in enumerate(enabled_packs, start=1):
        _scan_resource_source(source, f"resourcepacks/{source.name}", catalog)
        if progress:
            progress(65 + index * 25 / max(1, len(enabled_packs)), f"Применяю ресурспаки: {index}/{len(enabled_packs)}")
    return catalog, {
        "directory_name": root.name,
        "mods_scanned": len(jars),
        "lang_files_scanned": catalog.lang_files,
        "resourcepack_mode": resourcepack_mode,
        "enabled_resourcepacks": [path.name for path in enabled_packs],
        "sources": catalog.sources,
        "errors": catalog.errors[:200],
    }


def _safe_name(value: Any) -> str:
    result = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "")).strip("._")
    return result[:120] or "unknown"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _extract_server_export(archive_path: Path, target: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Серверный архив перевода содержит слишком много файлов.")
        total = 0
        root = target.resolve()
        for info in members:
            relative = Path(info.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Серверный архив перевода содержит небезопасный путь.")
            total += info.file_size
            if total > MAX_EXPORT_BYTES:
                raise ValueError("Серверный архив перевода слишком большой.")
            destination = (target / relative).resolve(strict=False)
            try:
                destination.relative_to(root)
            except ValueError as error:
                raise ValueError("Серверный архив перевода содержит небезопасный путь.") from error
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    export_root = target / "translation-export"
    if not export_root.is_dir():
        raise ValueError("Сервер вернул архив неизвестного формата.")
    return export_root


def _append_mod_tasks(
    export_root: Path,
    catalog: _Catalog,
    tasks: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    review_counts: dict[str, int] = {}
    english_namespaces = catalog.values["en_us"]
    russian_namespaces = catalog.values["ru_ru"]
    for namespace in sorted(english_namespaces):
        english = english_namespaces[namespace]
        russian = russian_namespaces.get(namespace, {})
        missing: dict[str, str] = {}
        reasons = {"missing": 0, "identical_to_english": 0, "contains_english": 0}
        for key, english_text in english.items():
            current = russian.get(key)
            category, reason = _translation_decision(english_text, current, key)
            if category == "review_required":
                review_counts[namespace] = review_counts.get(namespace, 0) + 1
                reviews.append({
                    "review_id": f"review-{len(reviews) + 1:06d}",
                    "kind": "mod_language",
                    "reason": reason,
                    "source_file": catalog.origins["en_us"].get(namespace, {}).get(key),
                    "target_file": f"assets/{namespace}/lang/ru_ru.json",
                    "namespace": namespace,
                    "key": key,
                    "source_text": english_text,
                    "current_text": current,
                })
                continue
            if category != "needs_translation":
                continue
            reasons[reason] += 1
            missing[key] = english_text
            if len(tasks) < MAX_TASKS:
                tasks.append({
                    "task_id": f"translation-{len(tasks) + 1:06d}",
                    "kind": "mod_language",
                    "reason": reason,
                    "source_file": catalog.origins["en_us"].get(namespace, {}).get(key),
                    "target_file": f"assets/{namespace}/lang/ru_ru.json",
                    "namespace": namespace,
                    "key": key,
                    "source_text": english_text,
                    "current_text": current,
                })
        if not missing:
            continue
        target = export_root / "mods" / _safe_name(namespace)
        _write_json(target / "en_us.json", english)
        _write_json(target / "current_ru_ru.json", russian)
        _write_json(target / "translation_template_ru_ru.json", {**russian, **missing})
        summaries.append({
            "namespace": namespace,
            "english_keys": len(english),
            "russian_keys": len(russian),
            "needs_translation": len(missing),
            "reasons": reasons,
            "template": str((target / "translation_template_ru_ru.json").relative_to(export_root)).replace("\\", "/"),
        })
    return {
        "jars_scanned": 0,
        "lang_files_scanned": catalog.lang_files,
        "incomplete": summaries,
        "review_required": len(reviews),
        "review_namespaces": review_counts,
        "errors": catalog.errors[:200],
    }


def build_combined_translation_export(
    client_directory: str | Path,
    server_archive: str | Path,
    destination: str | Path,
    *,
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    server_path = Path(server_archive).resolve(strict=True)
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_zip = target.with_name(target.name + ".part")
    with tempfile.TemporaryDirectory(prefix="server-control-client-translation-") as temporary:
        export_root = _extract_server_export(server_path, Path(temporary))
        if cancelled and cancelled():
            raise RuntimeError("Выгрузка перевода отменена.")
        catalog, client = scan_client_languages(client_directory, progress)
        if cancelled and cancelled():
            raise RuntimeError("Выгрузка перевода отменена.")
        try:
            tasks = json.loads((export_root / "translation_tasks.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Не удалось прочитать задания квестов из серверного архива.") from error
        if not isinstance(tasks, list):
            raise ValueError("Сервер вернул некорректный список заданий перевода.")
        shutil.rmtree(export_root / "mods", ignore_errors=True)
        reviews: list[dict[str, Any]] = []
        mods = _append_mod_tasks(export_root, catalog, tasks, reviews)
        try:
            manifest = json.loads((export_root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        statistics = manifest.get("statistics") if isinstance(manifest.get("statistics"), dict) else {}
        quests = statistics.get("quests") if isinstance(statistics.get("quests"), dict) else {}
        mods["jars_scanned"] = client["mods_scanned"]
        mods["resourcepack_mode"] = client["resourcepack_mode"]
        mods["enabled_resourcepacks"] = client["enabled_resourcepacks"]
        manifest.update({
            "format": "server-control-translation-export-v2",
            "client_scan": client,
            "statistics": {
                **statistics,
                "translation_tasks": len(tasks),
                "review_required": len(reviews),
                "task_limit_reached": len(tasks) >= MAX_TASKS,
                "mods": mods,
                "quests": quests,
            },
        })
        _write_json(export_root / "manifest.json", manifest)
        _write_json(export_root / "translation_tasks.json", tasks)
        _write_json(export_root / "review_required.json", reviews)

        instance = manifest.get("instance") if isinstance(manifest.get("instance"), dict) else {}
        quest_files = int(quests.get("files_with_tasks") or 0)
        report = [
            "# Проверка перевода Minecraft-сборки",
            "",
            f"Сборка: {instance.get('name') or instance.get('id') or 'Minecraft'}",
            "",
            f"- Клиентская папка: `{client['directory_name']}`",
            f"- Просканировано клиентских JAR: {client['mods_scanned']}",
            f"- Учтено активных ресурспаков: {len(client['enabled_resourcepacks'])}",
            f"- Модов/пространств с неполным итоговым переводом: {len(mods['incomplete'])}",
            f"- Файлов серверных квестов с английским текстом: {quest_files}",
            f"- Всего заданий на перевод: {len(tasks)}",
            f"- Сомнительных смешанных строк для отдельной проверки: {len(reviews)}",
            "",
            "## Неполные итоговые переводы модов",
            "",
        ]
        report.extend(
            f"- `{item['namespace']}`: {item['needs_translation']} строк → `{item['template']}`"
            for item in mods["incomplete"]
        )
        report.extend([
            "",
            "## Как использовать архив",
            "",
            "Передайте весь ZIP для перевода. Переводится только source_text; task_id, ключи, пути, плейсхолдеры и структура файлов сохраняются.",
            "Переводы модов рассчитаны по итоговым ресурсам клиента: встроенные lang-файлы, KubeJS/OpenLoader и активные ресурспаки.",
            "Квесты взяты с сервера, поэтому соответствуют выбранной серверной сборке.",
            "Основные задания находятся в translation_tasks.json. Сомнительные смешанные строки вынесены в review_required.json и автоматически переводить их не нужно.",
            "",
        ])
        (export_root / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
        (export_root / "README.txt").write_text(
            "Отправьте этот ZIP в ChatGPT и попросите перевести задания из translation_tasks.json.\n"
            "review_required.json содержит только сомнительные строки для ручной проверки.\n"
            "Не меняйте task_id, ключи, пути, управляющие коды и плейсхолдеры.\n",
            encoding="utf-8",
        )
        if progress:
            progress(95, "Упаковываю итоговый архив…")
        try:
            with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                for path in sorted(export_root.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        output.write(path, (Path("translation-export") / path.relative_to(export_root)).as_posix())
            if temporary_zip.stat().st_size > MAX_EXPORT_BYTES:
                raise ValueError("Архив перевода больше допустимого размера 512 MB.")
            os.replace(temporary_zip, target)
        finally:
            temporary_zip.unlink(missing_ok=True)
    return {
        "local_path": str(target),
        "size": target.stat().st_size,
        "tasks": len(tasks),
        "mods_incomplete": len(mods["incomplete"]),
        "quest_files": quest_files,
        "review_required": len(reviews),
        "task_limit_reached": len(tasks) >= MAX_TASKS,
        "client": client,
    }


def discover_client_instances() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    localappdata = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    userprofile = Path(os.environ.get("USERPROFILE") or Path.home())
    roots = [
        appdata / ".minecraft",
        userprofile / "curseforge" / "minecraft" / "Instances",
        appdata / "PrismLauncher" / "instances",
        appdata / "ATLauncher" / "instances",
        appdata / "com.modrinth.theseus" / "profiles",
        localappdata / ".minecraft",
    ]
    candidates: list[Path] = []
    standard = roots[0]
    if (standard / "mods").is_dir():
        candidates.append(standard)
    versions = standard / "versions"
    if versions.is_dir():
        try:
            candidates.extend(path for path in versions.iterdir() if (path / "mods").is_dir())
        except OSError:
            pass
    for root in roots[1:]:
        if not root.is_dir():
            continue
        try:
            for path in root.iterdir():
                if (path / "mods").is_dir():
                    candidates.append(path)
                if (path / ".minecraft" / "mods").is_dir():
                    candidates.append(path / ".minecraft")
                if (path / "minecraft" / "mods").is_dir():
                    candidates.append(path / "minecraft")
        except OSError:
            continue
    unique: dict[str, Path] = {}
    for path in candidates:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        unique[str(resolved).casefold()] = resolved
    return sorted(unique.values(), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
