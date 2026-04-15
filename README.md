# Local Sync Dashboard

Текущий релиз: `v1.2.0`

Локальный веб-инструмент для macOS, который помогает безопасно настраивать и запускать однонаправленные выгрузки между:

- `S3` и `S3-compatible` хранилищами
- `Яндекс Диском`
- локальными папками
- смонтированными папками `Synology`
- локальными папками `iCloud Drive` и `Google Drive for Desktop`

Проект задуман как удобная локальная панель управления поверх `rclone`: приложение хранит конфигурацию, показывает статусы, запускает задачи по расписанию и сохраняет историю запусков.

## Релиз `v1.2.0`

- orphan-run reaper: каждые 5 минут проверяет зависшие `running`-запуски без живого потока и помечает их `interrupted`
- SIGKILL после grace period: если `rclone` не завершается по `SIGTERM` (например, D-state от stale SMB mount), через 5 секунд отправляется `SIGKILL`
- широкая обработка исключений в `_execute_job`: неперехваченные исключения теперь записывают статус `FAILED` в БД, а не остав��яют `running` навсегда
- безопасный I/O в stream-цикле: `readline()` и `stream.read()` обёрнуты в `try/except`, чистка selector — в `finally`

## Релиз `v1.1.1`

- исправлена блокировка UI при активных rclone-задачах: `location.reload()` заменён на AJAX-поллинг `/api/runtime/jobs`
- добавлен TTL-кеш (30с) для `collect_setup_diagnostics()`

## Релиз `v1.1.0`

- интерфейс перестроен в `Control Room`-стиле: `Status / Route / Next Action`
- задачи теперь можно редактировать и клонировать прямо из списка и из истории запусков
- форма задач показывает `effective source/target route` до запуска
- для source/target появились быстрые browse-подсказки по подпапкам
- для Synology маршрутов появилась защита от типовой ошибки `/home/...` и от дублирования `source_path`
- repeated failed runs теперь читаются как operational incident, а не как набор разрозненных логов
- shell и основные экраны приведены к единому компактному ритму: `/`, `/jobs`, `/runs`, `/profiles`, `/setup`, `/ops`
- вкладка `Статус` убрана из основной навигации, operational readiness перенесен на главную

## Что уже умеет MVP

- создавать профили источников и назначений
- работать с локальными папками, `Synology`, `S3` и `Яндекс Диском`
- хранить дополнительные секреты в `macOS Keychain`, а не в `SQLite`
- перепроверять профиль вручную и получать более понятные диагностические подсказки
- сохранять результат последней проверки профиля и показывать его в UI
- подсказывать разные шаги проверки для `S3`, `Yandex` и локальных путей
- показывать action-oriented CTA в диагностике профиля: `Открыть setup`, `Исправить профиль`, `Проверить снова`
- запускать однонаправленные задачи в режиме `copy`
- запускать задачи вручную и по расписанию
- редактировать существующие задачи и создавать новые как копию проблемного маршрута
- ставить задачи на паузу и возобновлять их
- удалять задачи из интерфейса
- сохранять историю запусков и логи
- показывать live progress для активных app-managed задач
- делать `dry-run preview` для сохраненной задачи перед реальным запуском
- показывать `effective route` и блокировать заведомо ошибочные Synology маршруты до реального запуска
- сохранять последний известный runtime state активной задачи
- после рестарта переводить незавершенные `running`-запуски в `interrupted`
- давать recovery flow для `interrupted` запусков из `/runs` и `/jobs`
- показывать setup-диагностику по локальным путям и `rclone remotes`
- явно предупреждать в `/setup` и `/jobs`, что cloud-to-cloud маршруты идут через локальный `Mac`
- показывать pre-run checklist для длинных cloud маршрутов при составлении задачи
- держать `/profiles` и `/jobs` в list-first UX: список как основной экран, а создание в раскрывающемся блоке сверху
- использовать единый icon-button UX для основных действий с `title`/tooltip и логотипом в стиле favicon рядом с названием проекта

## Что важно про безопасность

MVP намеренно ограничен:

- используется только `copy`, без `sync`
- нет автоматических удалений на target
- одновременно выполняется только одна задача
- cloud-интеграции делегированы `rclone`

Это снижает риск случайной потери данных при первых настройках.

## Как сейчас идет перенос данных

Текущая архитектура не делает server-side copy между облаками.

- `rclone` запускается локально на вашем `Mac`
- данные читаются источником локальным процессом
- затем этот же локальный процесс отправляет их в target

Пример: перенос `S3 -> Яндекс Диск` сейчас идет через локальный `MacBook`, а не напрямую между двумя облаками.

Из этого следуют практические ограничения:

- скорость зависит от локального интернета и состояния `Mac`
- sleep или перезагрузка `Mac` остановят текущий перенос
- повторный запуск `rclone copy` обычно докачивает только недостающее, без удаления уже переданных файлов
- после рестарта приложение сохраняет последний известный прогресс и помечает run как `interrupted`
- продуктовая модель recovery: повторный `rclone copy` с дельты; `re-attach` к уже живому процессу не поддерживается

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
uvicorn app.main:app
```

Для реальных длинных переносов запускайте приложение без `--reload`: это локальный операционный UI, и любой dev-reload или ручной рестарт оборвёт текущую задачу, после чего она восстановится только как `interrupted`.

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
- `app/static/favicon.svg` — favicon приложения
- `tests/` — unit-тесты

## Поддерживаемые профили

### Local Folder

Обычная локальная папка, включая:

- `iCloud Drive`
- `Google Drive for Desktop`
- любые каталоги на диске Mac

### Synology Share

В MVP ожидается уже смонтированная папка SMB/NFS. Приложение не монтирует шару само.

Для профиля нужно указывать локальный путь macOS из `/Volumes`, а не `smb://...` и не внутренний путь NAS.

Пример:

- Finder / Connect to Server: `smb://Synology_Max._smb._tcp.local/home/Ext_HDD`
- в профиле приложения: `/Volumes/home/Ext_HDD`
- или root профиля: `/Volumes/home`, а в задаче `source_path=Ext_HDD`

Неправильно:

- `smb://Synology_Max._smb._tcp.local/home/Ext_HDD`
- `/home`
- `/home/Ext_HDD`

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
- runtime snapshots активных задач: `./var/runtime/`

Путь можно переопределить переменной окружения:

`LOCAL_SYNC_DATA_DIR`

## Автозапуск через launchd

В проекте есть шаблон:

[launchd/com.localsync.dashboard.plist](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/launchd/com.localsync.dashboard.plist)

Перед использованием нужно подставить абсолютные пути к проекту и `.venv`.

## Документация

- [docs/PROJECT.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/docs/PROJECT.md) — обзор архитектуры и текущего состояния проекта
- [docs/BACKLOG.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/docs/BACKLOG.md) — backlog с приоритетами
- [CHANGELOG.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/CHANGELOG.md) — зафиксированные релизные изменения
- [docs/plans/2026-04-13-job-progress-visibility.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/docs/plans/2026-04-13-job-progress-visibility.md) — план по progress bar, ETA и live-статусам задач
- [docs/plans/2026-04-15-vps-relay-research.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/docs/plans/2026-04-15-vps-relay-research.md) — исследование варианта с always-on relay / VPS вне MVP
- [agents.md](/Users/maksim/Documents/Projects/Mini%20Tasks/S3_Synology-to-Yandex_DIsk/agents.md) — локальные правила для агентной разработки
- `/ops` — встроенный операционный runbook по ночным запускам, recovery, `launchd` и backup/restore

## Полезные команды

### Локальные проверки

```bash
.venv/bin/python -m compileall app tests
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles tests.test_rclone tests.test_jobs tests.test_runtime_recovery
```

### Полезные команды rclone

```bash
rclone listremotes
rclone about yadisk:
rclone lsf "S3 Beget:bucket-name" --max-depth 1
```

## Текущее состояние

Проект уже пригоден для реальной локальной эксплуатации, но несколько вещей еще в работе:

- backup/restore конфигурации без секретов
- нотификации и более практичный `launchd`-операционный слой
- guided setup для `rclone remote`

## Ограничения

- нет двусторонней синхронизации
- нет delete propagation
- нет conflict resolution
- текущий restart-safe режим умеет фиксировать прерывание и последний известный прогресс; восстановление идет через повторный `copy` с дельты, а не через `re-attach`; orphan-run reaper проверяет зависшие запуски каждые 5 минут
- нет production-grade multi-user сценариев

## Лицензия и публикация

Репозиторий публикуется как локальный macOS utility project. Если захотите, следующим шагом можно добавить `LICENSE`, релизные теги и более формальный changelog.
