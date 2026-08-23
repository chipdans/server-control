# Обновление существующей установки 0.3.x до 1.0.2

Этот порядок рассчитан на уже работающий проект `chipdans/server-control`. Он
сохраняет существующих пользователей D1, URL Worker, Yandex secrets,
`AGENT_API_KEY`, RCON password, конфигурацию Agent и текущую Dragonfyre-сборку.

## Что изменится

- D1 получит только новые таблицы/индексы/столбцы из миграции `0004`.
- Worker начнёт использовать приватный R2 для больших временных передач.
- Agent перейдёт с одного файла версии 1.x на проверяемый release bundle 2.0,
  сохранив `/etc/server-control/agent-config.json`.
- Desktop обновится до 1.0 и сохранит `server-control.json` и локальные UI
  preferences.

Сделайте один обычный backup мира до начала. Не выключайте питание сервера во
время миграции или первой установки Agent.

## Полный порядок

### A. Cloudflare

В обычной Windows PowerShell:

```powershell
cd D:\Code\server\worker

npx wrangler r2 bucket create server-control-files
npx wrangler r2 bucket create server-control-files-preview
npx wrangler d1 migrations apply server-control --remote
npx wrangler deploy
```

Если Wrangler сообщает, что bucket уже существует, это не ошибка — переходите
к следующей команде. При миграции должна примениться
`0004_control_plane.sql`. Повторный запуск миграций безопасен: уже применённые
файлы пропускаются.

Проверьте:

```powershell
curl.exe https://server-control-hub.channelchipdanq.workers.dev/health
```

### B. Создать релиз

После публикации итогового коммита в `main`:

```powershell
cd D:\Code\server
git fetch origin
git tag -a v1.0.2 origin/main -m "Server Control 1.0.2"
git push origin v1.0.2
```

В GitHub Actions дождитесь зелёных jobs `validate` и `build`. В Releases должны
появиться три ZIP и три файла SHA-256. Если workflow красный, Agent и desktop не
обновляйте: исправляется исходная причина, затем проверки запускаются заново.

### C. Установить Agent 2.0 на Debian

Первый переход нужен вручную, потому что Agent 1.x ещё не понимает новую
задачу безопасного самообновления. В SSH выполните весь блок:

```bash
cd /tmp
curl -fL https://github.com/chipdans/server-control/releases/download/v1.0.2/ServerControl-Agent.zip -o ServerControl-Agent.zip
curl -fL https://github.com/chipdans/server-control/releases/download/v1.0.2/ServerControl-Agent.zip.sha256 -o ServerControl-Agent.zip.sha256
sha256sum -c ServerControl-Agent.zip.sha256
install -d -m 0700 /tmp/server-control-agent-1.0.0
unzip -q ServerControl-Agent.zip -d /tmp/server-control-agent-1.0.0
sudo sh /tmp/server-control-agent-1.0.0/install-agent.sh
sudo systemctl --no-pager --full status server-control-agent.service
```

Ожидается `Active: active (running)`. Установщик:

- не перезаписывает существующий `agent-config.json`;
- создаёт отдельный release-каталог и атомарную ссылку `current`;
- устанавливает проверенные systemd units/helpers;
- сохраняет Minecraft владельцем файлов и выдаёт Agent только group access;
- проверяет manifest и SHA-256 каждого файла.

Если старая конфигурация содержит только Dragonfyre, Agent автоматически создаст
для неё совместимый первый профиль. После запуска откройте **Обновления** и
убедитесь, что показаны Agent `2.0.2`, protocol `2`.

### D. Обновить Windows client

Можно запустить текущий `ServerControl.exe`: он найдёт `v1.0.2`, проверит
SHA-256, обновит client и updater, перезапустится и подтвердит health marker.
При сбое он вернёт предыдущий EXE и запишет
`ServerControl-update-error.log` рядом с программой.

Если автоматическое обновление не стартовало, выполните ручную замену один раз:

1. Закройте Server Control.
2. Скачайте `ServerControl-Setup.zip` из release `v1.0.2`.
3. Сверьте опубликованный SHA-256.
4. Скопируйте `ServerControl.exe` и `ServerControlUpdater.exe` с заменой в
   `%LOCALAPPDATA%\ServerControl`.
5. Не заменяйте существующий `server-control.json`.

### E. Финальная проверка

1. В заголовке клиента указано `Server Control 1.0.0`.
2. **Главная** обновляется, связь с Agent online, protocol 2.
3. Dragonfyre виден в **Сборки**, а его путь/RCON/служба соответствуют старой
   конфигурации.
4. Команда `list` даёт один `[RCON]` ответ; строки запуска RCON client не спамят
   консоль.
5. Создайте ручной backup и скачайте его.
6. Создайте небольшой текстовый файл через **Файлы**, сохраните, скачайте и
   удалите его.
7. Запустите безопасную диагностику `uptime`.
8. Проверьте роль Viewer отдельной тестовой учётной записью.

## Откат

### Desktop

Updater делает автоматический rollback. Ручная предыдущая копия находится рядом
с EXE как `ServerControl.previous.exe`.

### Agent

Последующие обновления Agent автоматически возвращают предыдущую ссылку и
system files, если service + Hub health не подтверждены. Для первого перехода
старый файл `/opt/server-control/server_control_agent.py` не удаляется
установщиком, но новый unit использует `/opt/server-control/current/...`.

### Worker

Миграция `0004` расширяющая: старый Worker не использует новые таблицы, поэтому
его код можно временно развернуть снова. Не удаляйте таблицы `jobs`, `transfers`
или R2 objects вручную; scheduled cleanup выполнит retention после возврата 1.0.
