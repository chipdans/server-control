# Установка Server Control

## 1. Что понадобится

- учётная запись Cloudflare на бесплатном тарифе;
- GitHub-репозиторий для релизов приложения;
- доступ к Debian-серверу `ChipdanServer`;
- OAuth-токен Яндекс Умного дома и ID умной розетки, которая питает сервер.

Не публикуйте OAuth-токен, RCON-пароль, ключ агента или `BOOTSTRAP_KEY` в
GitHub, Discord, скриншотах или конфигурации клиента.

## 2. Развернуть бесплатный Control Hub

Установите Node.js LTS на своём ПК, откройте терминал в папке `worker` и
выполните:

```bash
npx wrangler login
npx wrangler d1 create server-control
```

Во втором ответе Cloudflare покажет `database_id`. Вставьте его вместо
`REPLACE_WITH_D1_DATABASE_ID` в `worker/wrangler.toml`.

Далее создайте таблицы и секреты:

```bash
npx wrangler d1 migrations apply server-control --remote
npx wrangler secret put JWT_SECRET
npx wrangler secret put BOOTSTRAP_KEY
npx wrangler secret put AGENT_API_KEY
npx wrangler secret put YANDEX_OAUTH_TOKEN
npx wrangler secret put YANDEX_DEVICE_ID
npx wrangler deploy
```

Для `JWT_SECRET`, `BOOTSTRAP_KEY` и `AGENT_API_KEY` сгенерируйте разные
случайные строки длиной не менее 32 байт. Например:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Сохраните только для себя `BOOTSTRAP_KEY` и `AGENT_API_KEY`. После создания
аккаунта владельца `BOOTSTRAP_KEY` больше не работает, потому что Worker не
разрешает второй первоначальный аккаунт.

Worker использует официальный API Яндекс Умного дома: он читает состояние
розетки по `GET /v1.0/devices/{device_id}` и отправляет действие по
`POST /v1.0/devices/actions`. Поэтому ему нужен OAuth-токен и ID именно той
розетки, которая питает домашний сервер.

## 3. Подготовить Minecraft на Debian

Текущая сборка расположена в `/opt/minecraft/dragonfyre`.

1. В `server.properties` включите RCON и задайте новый длинный пароль:

   ```properties
   enable-rcon=true
   rcon.port=25575
   rcon.password=ДЛИННЫЙ_СЛУЧАЙНЫЙ_ПАРОЛЬ
   ```

2. Не открывайте и не пробрасывайте порт `25575` в интернет. Агент подключается
   к нему только по `127.0.0.1`.
3. В `variables.txt` у Dragonfyre установите `RESTART=false`, иначе команда
   остановки Minecraft может запустить его снова.
4. Скопируйте `agent/minecraft-dragonfyre.service.example` в
   `/etc/systemd/system/minecraft-dragonfyre.service`, затем выполните:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now minecraft-dragonfyre.service
   ```

Перед включением убедитесь, что сервер нормально запускается и останавливается:

```bash
sudo systemctl status minecraft-dragonfyre.service
sudo systemctl stop minecraft-dragonfyre.service
sudo systemctl start minecraft-dragonfyre.service
```

## 4. Установить агента

На `ChipdanServer`:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin servercontrol
sudo install -d -o servercontrol -g servercontrol /opt/server-control /etc/server-control
sudo install -o servercontrol -g servercontrol -m 0755 agent/server_control_agent.py /opt/server-control/server_control_agent.py
sudo install -o servercontrol -g servercontrol -m 0600 agent-config.json /etc/server-control/agent-config.json
sudo install -o root -g root -m 0440 agent/servercontrol-sudoers.example /etc/sudoers.d/servercontrol
sudo install -o root -g root -m 0644 agent/server-control-agent.service /etc/systemd/system/server-control-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now server-control-agent.service
```

Перед командой с `agent-config.json` создайте его из
`agent/config.example.json` и заполните:

- `hub_url` — URL Worker после `wrangler deploy`;
- `agent_api_key` — тот же, что добавлен в Cloudflare Secrets;
- `minecraft.rcon_password` — пароль из `server.properties`;
- при необходимости имя systemd-службы и allow-list Linux-команд.

Проверьте агент:

```bash
sudo systemctl status server-control-agent.service
sudo journalctl -u server-control-agent.service -f
```

Если лог Minecraft недоступен агенту, добавьте пользователя `servercontrol` в
группу `minecraft`, затем перезапустите агент:

```bash
sudo usermod -aG minecraft servercontrol
sudo systemctl restart server-control-agent.service
```

## 5. Собрать и установить Windows-клиент

В GitHub создайте релиз с тегом `v0.1.0`: workflow
`.github/workflows/release.yml` соберёт `ServerControl-Setup.zip` и
`ServerControl-Update.zip`.

Распакуйте `ServerControl-Setup.zip` в папку без ограничений на запись, например
`%LOCALAPPDATA%\ServerControl`. Рядом с `ServerControl.exe` откройте
`server-control.json` и укажите:

```json
{
  "api_base_url": "https://ВАШ-WORKER.workers.dev",
  "update": {
    "enabled": true,
    "install_automatically": true,
    "repository": "ВАШ-GITHUB/server-control-releases",
    "asset_name": "ServerControl-Update.zip"
  }
}
```

Для автоматических обновлений репозиторий с release-архивами должен быть
доступен пользователям приложения без GitHub-токена. Его можно сделать
отдельным публичным репозиторием, содержащим только файлы релизов; исходный код
при этом можно оставить в другом приватном репозитории. В обновлениях нет
секретов.

Если исходный репозиторий закрытый, создайте публичный
`server-control-releases`, а в GitHub исходного проекта добавьте:

- переменную Actions `RELEASE_REPOSITORY` со значением
  `ваш-аккаунт/server-control-releases`;
- секрет Actions `RELEASE_REPOSITORY_TOKEN` — fine-grained token с правом
  **Contents: Read and write** только для репозитория релизов.

Workflow сам загрузит оба архива в этот публичный репозиторий. Если исходный
репозиторий публичный, ничего добавлять не нужно: обновления будут публиковаться
в нём же.

## 6. Создать владельца и пользователей

1. Запустите `ServerControl.exe`.
2. Нажмите **Первичная настройка**.
3. Введите сохранённый `BOOTSTRAP_KEY`, логин владельца и пароль от 12 символов.
4. Войдите в приложение и во вкладке **Пользователи** создайте остальных людей.
5. Отключение пользователя немедленно отзывает его текущий сеанс. Старый EXE,
   сохранённый пароль или отсутствие обновления не дадут ему отправить команду.

## 7. Как работает безопасное выключение

Кнопка **Безопасно выключить** не обесточивает сервер сразу. Она ставит задачу
агенту: отправить `save-all flush`, остановить Minecraft, дождаться завершения
службы и выполнить `sync`. Только после успешного ответа агенту Worker посылает
умной розетке команду выключения. Кнопка **Отключить сразу** доступна только
владельцу и предназначена для аварийных случаев.
