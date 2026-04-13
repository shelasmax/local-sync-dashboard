# Project Overview

## Что это

`local-sync-dashboard` — локальная панель синхронизации для macOS. Она управляет однонаправленными задачами выгрузки между локальными папками, смонтированными шарами Synology и cloud remote через `rclone`.

Текущий MVP делает ставку на безопасность:

- только режим `copy`
- без автоматических удалений
- один активный job одновременно
- секреты не хранятся в SQLite

## Текущий стек

- `FastAPI` — HTTP API и серверный HTML UI
- `Jinja2` — шаблоны интерфейса
- `SQLAlchemy` + `SQLite` — состояние приложения
- `APScheduler` — фоновые расписания
- `rclone` — фактический движок переноса данных
- `macOS Keychain` — хранение пользовательских секретов

## Структура проекта

- `app/main.py` — HTTP-маршруты, HTML-страницы, API и glue code
- `app/models.py` — модели `StorageProfile`, `SyncJob`, `RunHistory`, `AppSettings`
- `app/services/profiles.py` — создание, обновление, удаление и проверка профилей
- `app/services/jobs.py` — очередь, scheduler, запуск и история задач
- `app/services/rclone.py` — сборка и исполнение команд `rclone`
- `app/templates/` — HTML UI
- `app/static/style.css` — общий стиль интерфейса
- `tests/` — unit-тесты на helpers, discovery и profile service
- `launchd/` — шаблон `launchd`-агента

## Сущности данных

### `StorageProfile`

Хранит подключение к источнику или назначению:

- имя профиля
- тип профиля
- корневой путь или `rclone remote`
- статус доступности
- ссылку на секрет в Keychain
- `options_json`

### `SyncJob`

Описывает однонаправленную задачу:

- `source_profile_id`
- `target_profile_id`
- подпути источника и назначения
- расписание
- флаги `enabled` и `verify_checksum`
- фильтры
- ограничение скорости

### `RunHistory`

Хранит факт запуска:

- итоговый статус
- summary
- лог-файл
- `stdout/stderr`
- preview команды
- байты и количество файлов

## Как сейчас работает запуск job

1. UI или API создает `SyncJob`.
2. `JobRunner` регистрирует расписание в `APScheduler`.
3. На запуске строится команда `rclone copy`.
4. Результат пишется в `RunHistory` и лог-файл в `var/logs/`.
5. В UI доступны список запусков и детальная карточка run.

## Текущее состояние продукта

### Уже есть

- русифицированный UI
- setup-диагностика по локальным папкам и `rclone remotes`
- создание, редактирование и удаление профилей
- безопасные проверки на дубли и на удаление профиля, который уже используется
- CRUD задач
- ручной запуск, pause/resume и запуск по расписанию
- S3-специфичные поля профиля в UI
- история запусков и логирование

### Чего пока нет

- live progress bar в интерфейсе
- ETA и остаток по каждой текущей задаче
- предпросмотр dry-run
- двусторонняя синхронизация
- удаление задач из UI
- встроенная настройка `rclone remote` из интерфейса
- richer health/ops dashboard

## Операционные команды

### Локальный запуск

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

### Проверки

```bash
.venv/bin/python -m compileall app tests
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles
```

### Полезные `rclone` команды

```bash
rclone listremotes
rclone about yadisk:
rclone lsf "S3 Beget:bucket-name" --max-depth 1
```

## Хранение состояния

- БД приложения: `var/app.db`
- логи запусков: `var/logs/`
- временные ручные логи long-run переносов: сейчас использовался `/tmp/macbook-to-yadisk.log`

## Известные ограничения

- UI пока показывает только итоги run, а не live progress
- `S3 remote` в интерфейсе удобнее редактируется, но сам `rclone remote` все равно должен существовать заранее
- retry-логика завязана на временные сетевые ошибки и пока не выносится в UI-настройки
- часть UX еще рассчитана на ручную эксплуатацию, а не на долгоживущий production-like сервис

## Что логично делать дальше

1. Добавить live progress и ETA в UI по активным задачам.
2. Превратить текущий ручной `rclone` мониторинг в first-class runtime state.
3. Добавить удаление job и более удобную диагностику ошибок.
4. После этого доработать операционный слой: launchd, retention, backup/restore конфигурации.
