# Local Sync Dashboard

Локальный веб-инструмент для macOS, который помогает безопасно настраивать и запускать однонаправленные выгрузки между:

- `S3` и `S3-compatible` хранилищами
- `Яндекс Диском`
- локальными папками
- смонтированными папками `Synology`
- локальными папками `iCloud Drive` и `Google Drive for Desktop`

Проект задуман как удобная локальная панель управления поверх `rclone`: приложение хранит конфигурацию, показывает статусы, запускает задачи по расписанию и сохраняет историю запусков.

## Что уже умеет MVP

- создавать профили источников и назначений
- работать с локальными папками, `Synology`, `S3` и `Яндекс Диском`
- хранить дополнительные секреты в `macOS Keychain`, а не в `SQLite`
- запускать однонаправленные задачи в режиме `copy`
- запускать задачи вручную и по расписанию
- ставить задачи на паузу и возобновлять их
- сохранять историю запусков и логи
- показывать setup-диагностику по локальным путям и `rclone remotes`

## Что важно про безопасность

MVP намеренно ограничен:

- используется только `copy`, без `sync`
- нет автоматических удалений на target
- одновременно выполняется только одна задача
- cloud-интеграции делегированы `rclone`

Это снижает риск случайной потери данных при первых настройках.

## Требования

- `macOS`
- `Python 3.11+`
- установленный `rclone`
- настроенные `rclone remotes` для нужных облаков

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Зависимость `python-multipart` подтягивается автоматически и нужна для HTML-форм в интерфейсе.

## Запуск

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

После старта откройте:

`http://127.0.0.1:8000`

## Как устроен проект

### Основной стек

- `FastAPI` — HTTP API и серверный HTML UI
- `Jinja2` — шаблоны интерфейса
- `SQLAlchemy` + `SQLite` — локальное состояние приложения
- `APScheduler` — фоновые расписания
- `rclone` — фактический перенос файлов
- `macOS Keychain` — хранение пользовательских секретов

### Ключевые файлы

- `app/main.py` — маршруты UI и API
- `app/models.py` — модели БД
- `app/services/profiles.py` — логика профилей
- `app/services/jobs.py` — scheduler и запуски задач
- `app/services/rclone.py` — интеграция с `rclone`
- `app/templates/` — HTML UI
- `app/static/style.css` — стили
- `tests/` — unit-тесты

## Поддерживаемые профили

### Local Folder

Обычная локальная папка, включая:

- `iCloud Drive`
- `Google Drive for Desktop`
- любые каталоги на диске Mac

### Synology Share

В MVP ожидается уже смонтированная папка SMB/NFS. Приложение не монтирует шару само.

### S3 Remote

Профиль использует уже существующий `rclone remote`. В UI можно отдельно редактировать:

- имя remote
- bucket
- prefix
- provider
- region
- endpoint

Но сам `rclone remote` все равно должен быть создан заранее.

### Yandex Disk Remote

Профиль использует уже существующий `rclone remote`, например `yadisk:`.

## Где хранятся данные приложения

По умолчанию:

- БД: `./var/app.db`
- логи: `./var/logs/`

Путь можно переопределить переменной окружения:

`LOCAL_SYNC_DATA_DIR`

## Автозапуск через launchd

В проекте есть шаблон:

[launchd/com.localsync.dashboard.plist](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/launchd/com.localsync.dashboard.plist)

Перед использованием нужно подставить абсолютные пути к проекту и `.venv`.

## Документация

- [docs/PROJECT.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/docs/PROJECT.md) — обзор архитектуры и текущего состояния проекта
- [docs/BACKLOG.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/docs/BACKLOG.md) — backlog с приоритетами
- [docs/plans/2026-04-13-job-progress-visibility.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/docs/plans/2026-04-13-job-progress-visibility.md) — план по progress bar, ETA и live-статусам задач
- [agents.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/agents.md) — локальные правила для агентной разработки

## Полезные команды

### Локальные проверки

```bash
.venv/bin/python -m compileall app tests
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles
```

### Полезные команды rclone

```bash
rclone listremotes
rclone about yadisk:
rclone lsf "S3 Beget:bucket-name" --max-depth 1
```

## Текущее состояние

Проект уже пригоден для реальной локальной эксплуатации, но несколько вещей еще в работе:

- live progress bar по активным задачам
- ETA и остаток по объему/файлам прямо в UI
- dry-run перед первым переносом
- удаление задач из интерфейса
- более богатая диагностика ошибок

## Ограничения

- нет двусторонней синхронизации
- нет delete propagation
- нет conflict resolution
- нет production-grade multi-user сценариев

## Лицензия и публикация

Репозиторий публикуется как локальный macOS utility project. Если захотите, следующим шагом можно добавить `LICENSE`, релизные теги и более формальный changelog.
