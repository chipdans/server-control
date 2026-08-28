# Установка Server Control 2

Server Control 2 оставляет Control Hub только для входа, прав и состояния. Обе консоли после входа подключаются к домашнему серверу напрямую по SSH.

## Что будет настроено

- внешний SSH: `46.175.223.107:2222` → отдельный listener `192.168.0.108:2222`;
- обычный локальный вход `chipdan@192.168.0.108:22` не публикуется и не меняется;
- отдельный Linux-пользователь `servercontrol-admin` с входом только по ключу и полным `sudo`;
- отдельный `servercontrol-minecraft` со вторым ключом и принудительным запуском только Dragonfyre tmux;
- две консоли в приложении: `sudo -i` и `tmux attach-session -t dragonfyre`;
- Minecraft запускается службой `dragonfyre.service` внутри tmux;
- RCON отключается в `server.properties` и в конфигурации Agent;
- права приложения: состояние, Linux-консоль, Minecraft-консоль, питание, пользователи;
- владелец может менять собственный логин и пароль.

## 1. Debian — выполнить одним блоком

Сначала предупредите игроков: если Minecraft сейчас запущен, установщик корректно остановит его, заменит службу и снова запустит.

```bash
set -e
cd /tmp
curl -fL https://github.com/chipdans/server-control/archive/refs/heads/v2.tar.gz -o server-control-v2.tar.gz
tar -xzf server-control-v2.tar.gz
cd server-control-v2/agent
sudo sh install-v2-console.sh
sudo systemctl status dragonfyre.service --no-pager -l
sudo systemctl status server-control-agent.service --no-pager -l
sudo cat /root/server-control-v2-ssh-info.txt
```

Ожидаемый итог:

- `Server Control 2 SSH настроен.`;
- `dragonfyre.service` — `active (running)`, если он работал до установки;
- `server-control-agent.service` — `active (running)`;
- в `/root/server-control-v2-ssh-info.txt` указаны адрес, порт, два технических логина и отпечаток сервера;
- два приватных ключа находятся в `/home/chipdan/server-control-v2-linux-private-key` и `/home/chipdan/server-control-v2-minecraft-private-key` для последующего SCP.

Если Minecraft до установки был остановлен и его нужно запустить:

```bash
sudo systemctl start dragonfyre.service
sudo systemctl is-active dragonfyre.service
sudo -u minecraft tmux list-sessions
```

Ожидается `active` и сессия `dragonfyre`.

## 2. Роутер — одно правило

Создайте правило перенаправления порта:

| Поле | Значение |
|---|---|
| Название | `Server Control SSH` |
| Протокол | `TCP` |
| Внешний порт | `2222` |
| Внутренний IP | `192.168.0.108` |
| Внутренний порт | `2222` |

Порт `25565` не меняется — он остаётся только для Minecraft.
На внутреннем порту `2222` SSH принимает только `servercontrol-admin` по ключу. Обычный порт `22` для локального входа `chipdan` продолжает работать отдельно.

После сохранения правила проверьте из Windows:

```powershell
Test-NetConnection 46.175.223.107 -Port 2222
```

Ожидается `TcpTestSucceeded : True`. Проверку лучше делать не из домашнего Wi-Fi, а через мобильный интернет или другую внешнюю сеть, если роутер не поддерживает NAT loopback.

## 3. Windows и Cloudflare Worker — выполнить одним блоком PowerShell

В первой строке укажите путь к старому локальному репозиторию, где уже записан настоящий D1 `database_id`. Блок извлечёт только этот ID и отдельно скачает чистую ветку v2, поэтому незакоммиченные файлы старого приложения не затрагиваются.

```powershell
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$OldRepo = "D:\Code\server-control"
$Server = "chipdan@192.168.0.108"
$Download = Join-Path $env:USERPROFILE "Downloads\ServerControlV2"
$Source = Join-Path $Download "source"

$OldWrangler = Get-Content "$OldRepo\worker\wrangler.toml" -Raw
$D1Match = [regex]::Match($OldWrangler, '(?m)^\s*database_id\s*=\s*"([^"]+)"')
if (-not $D1Match.Success -or $D1Match.Groups[1].Value -eq "REPLACE_WITH_D1_DATABASE_ID") {
    throw "В старом wrangler.toml не найден настоящий D1 database_id."
}
$D1Id = $D1Match.Groups[1].Value

New-Item -ItemType Directory -Force $Download | Out-Null
if (Test-Path "$Source\.git") {
    git -C $Source fetch origin
    git -C $Source switch v2
    git -C $Source pull --ff-only origin v2
} elseif (Test-Path $Source) {
    throw "Папка $Source уже существует, но это не Git-репозиторий. Переименуйте её и повторите блок."
} else {
    git clone --branch v2 --single-branch https://github.com/chipdans/server-control.git $Source
}
$V2WranglerPath = "$Source\worker\wrangler.toml"
$V2Wrangler = (Get-Content $V2WranglerPath -Raw).Replace("REPLACE_WITH_D1_DATABASE_ID", $D1Id)
[System.IO.File]::WriteAllText($V2WranglerPath, $V2Wrangler, [System.Text.UTF8Encoding]::new($false))

scp "${Server}:/home/chipdan/server-control-v2-linux-private-key" "$Download\server-control-v2-linux-private-key"
scp "${Server}:/home/chipdan/server-control-v2-minecraft-private-key" "$Download\server-control-v2-minecraft-private-key"
scp "${Server}:/home/chipdan/server-control-v2-ssh-info.txt" "$Download\server-control-v2-ssh-info.txt"

$Info = Get-Content "$Download\server-control-v2-ssh-info.txt" -Raw | ConvertFrom-StringData
$LinuxPrivateKey = Get-Content "$Download\server-control-v2-linux-private-key" -Raw
$MinecraftPrivateKey = Get-Content "$Download\server-control-v2-minecraft-private-key" -Raw

Set-Location "$Source\worker"
npm install
npx wrangler whoami
$Info.SSH_HOST | npx wrangler secret put SSH_HOST
$Info.SSH_PORT | npx wrangler secret put SSH_PORT
$Info.SSH_LINUX_USERNAME | npx wrangler secret put SSH_LINUX_USERNAME
$Info.SSH_MINECRAFT_USERNAME | npx wrangler secret put SSH_MINECRAFT_USERNAME
$Info.SSH_HOST_KEY_SHA256 | npx wrangler secret put SSH_HOST_KEY_SHA256
$LinuxPrivateKey | npx wrangler secret put SSH_LINUX_PRIVATE_KEY
$MinecraftPrivateKey | npx wrangler secret put SSH_MINECRAFT_PRIVATE_KEY
npx wrangler deploy --dry-run
npx wrangler deploy --keep-vars

curl.exe --connect-timeout 10 --max-time 20 -sS -w "`nHTTP: %{http_code}`n" "https://server-control-hub.channelchipdanq.workers.dev/health"
```

Ожидаемый итог:

- `wrangler whoami` показывает подключённый аккаунт Cloudflare;
- каждая команда `secret put` завершается сообщением об успешной загрузке секрета;
- `wrangler deploy --dry-run` собирает Worker без ошибки;
- настоящий deploy возвращает адрес `server-control-hub.channelchipdanq.workers.dev`;
- `/health` возвращает JSON с `"ok":true` и `HTTP: 200`.

Закрытые SSH-ключи никогда не коммитятся в GitHub и не кладутся рядом с EXE. Приложение получает только ключ выбранной консоли после успешного входа с соответствующим правом и держит его в памяти активной сессии. Minecraft-ключ принудительно ограничен tmux даже при использовании вне приложения.

## 4. Проверка приложения

1. Войдите под владельцем.
2. На странице состояния должны появиться питание, Agent, домашний сервер и Dragonfyre.
3. Откройте `Консоли → Linux`: приглашение должно показывать root-shell после `sudo -i`.
4. Переключитесь на `Minecraft`: должна открыться текущая tmux-консоль Dragonfyre.
5. Создайте тестового пользователя только с правом Minecraft. У него не должна отображаться Linux-консоль.
6. Заблокируйте тестового пользователя: его уже выданный токен перестанет работать, а открытое приложение закроет консоли при ближайшей проверке сеанса.
