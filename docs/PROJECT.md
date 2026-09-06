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

Для `synology_share` значение `root_path_or_remote` должно быть уже смонтированным локальным путем macOS из `/Volumes`, а не `smb://...` и не путём на самой NAS. Например, для `smb://nas.local/media/photos` корректный профиль — `/Volumes/media/photos` или `/Volumes/media` с `source_path=photos` в задаче.

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
4. Во время выполнения `rclone` отдает JSON stats, а приложение держит live progress в runtime state.
5. Параллельно последний известный progress сохраняется в `var/runtime/`.
6. Результат пишется в `RunHistory` и лог-файл в `var/logs/`.
7. В UI доступны список запусков, live progress и детальная карточка run.

## Как на самом деле идет трафик

Важно явно фиксировать это для продукта и эксплуатации: текущий MVP не делает cloud-to-cloud перенос на стороне провайдеров.

Фактическая модель исполнения такая:

1. `rclone` запускается локально на `Mac`.
2. Локальный процесс читает данные из source.
3. Этот же локальный процесс отправляет данные в target.

Это касается и сценария `S3 -> Яндекс Диск`: трафик идет через локальную машину пользователя.

Практические последствия:

- скорость зависит от локального интернет-канала, CPU, диска и сетевого состояния `Mac`
- если `Mac` ушел в sleep или был перезагружен, текущий перенос остановится
- если после остановки снова запустить `rclone copy`, уже переданные файлы обычно будут пропущены, а перенос продолжится с дельты
- это безопаснее для MVP, чем строить сложную серверную инфраструктуру, но хуже по устойчивости для длинных job

## Что происходит при перезапуске

### Если перезагрузить `Mac`

- активный перенос остановится
- уже переданные данные не откатятся
- для продолжения нужно заново запустить job или ручную команду `rclone`

### Если перезапустить только веб-приложение

- текущий ручной `rclone` процесс может продолжить работать, если он был запущен отдельно от веб-процесса
- приложение больше не теряет контекст полностью: если активный app-managed run не завершился, он будет помечен как `interrupted`, а последний известный прогресс останется в истории
- полноценный `re-attach` к уже живому `rclone` процессу после restart не поддерживается по продуктовому решению
- recovery-модель проекта: повторный безопасный `copy` с дельты

### Если параллельно дорабатывать новую версию приложения

- обычные изменения кода не вредят уже идущему внешнему `rclone` процессу
- риск появляется в момент рестарта runtime, если активная задача запущена самим приложением и ее live-state не вынесен в устойчивое хранилище

## Текущее состояние продукта

Релизный срез на `2026-04-16`: `v2.1.1`

### Уже есть

- релиз `v1.1.0` с visual layer в формате `Control Room`
- режимы `create / edit / clone` для задач без отдельного split-view
- action-first карточки задач и запусков: `Исправить`, `Копировать как новую`, `Dry-run`, `Открыть лог`
- route preview для формы задачи с `effective source/target`
- browse API для выбора подпапок по local/Synology path и через `rclone lsf` для remote
- защита от типовой Synology-ошибки: `/home/...` вместо `/Volumes/...`
- защита от дублирования `source_path` поверх последнего сегмента корня профиля
- русифицированный UI
- setup-диагностика по локальным папкам и `rclone remotes`
- создание, редактирование и удаление профилей
- ручная перепроверка профиля из UI и API с обновлением статуса
- сохранение последнего результата проверки профиля внутри локального состояния приложения
- безопасные проверки на дубли и на удаление профиля, который уже используется
- CRUD задач
- удаление задач из UI и API с защитой от удаления активной задачи
- ручной запуск, pause/resume и запуск по расписанию
- job-level runtime-тюнинг `rclone`: `transfers`, `checkers` и `fast-list` как часть самой задачи для long-running и small-files сценариев
- явная timeout-диагностика для long-running запусков: timed-out run теперь показывает лимит времени и реально перенесённый объём до остановки
- S3-специфичные поля профиля в UI
- более понятные hints для локальных путей и `rclone remotes` на странице профилей
- явные подсказки в `/profiles` и `/setup`, что для `Synology SMB` нужно использовать mount path из `/Volumes`, а не `smb://...`
- последняя ошибка или успешный результат проверки видны прямо в строке профиля
- richer hints по типам ошибок `S3`, `Yandex` и local path
- action-oriented CTA в диагностике профиля: быстрые действия вместо одного только текста
- `dry-run preview` для сохраненной задачи перед реальным запуском
- история запусков и логирование
- live progress bar, ETA, скорость и текущие transfer items для активных app-managed задач
- recovery UX для `interrupted` запусков в `/runs`, `/runs/{id}`, на главной и в `/jobs`
- orphan-run reaper, который периодически переводит зависшие `running`-запуски в `interrupted`
- SIGKILL после grace period для зависших `rclone`-процессов на stale mount
- широкая обработка неожиданных исключений во время run execution и безопасный I/O в stream-цикле
- operational `/runs` плюс отдельный archive `/runs/archive` с pagination, фильтрами и cleanup-очисткой локальной run history
- явные `Скрыть` / `Показать` toggle-контролы для верхних recovery/incident-блоков на `/`, `/jobs`, `/runs` с сохранением состояния в браузере
- блок активных run на `/runs`, собранный из live runtime snapshots, а не только из БД
- profile diagnostics, связанные с конкретными сценариями `/ops` через helper-блоки и прямые ссылки на relevant runbook sections
- counters на `/profiles` и более action-oriented profile helper UX
- стабильные empty-state и более честные operational sections на `/runs`, даже когда live snapshot сейчас отсутствует
- cloud-to-cloud предупреждения в `/setup` и при составлении маршрута на `/jobs`
- pre-run checklist для длинных cloud job при составлении маршрута на `/jobs`
- встроенный экран `/ops` с runbook по запуску, recovery, `launchd` и backup/restore
- минимальная operational-настройка лимита одного запуска в `/ops`, чтобы длинные ночные выгрузки не требовали ручной правки SQLite
- list-first раскладка на `/profiles` и `/jobs`: основной список в широких горизонтальных блоках, а форма создания спрятана в раскрывающийся блок сверху
- единый визуальный ритм shell и карточек на `/`, `/jobs`, `/runs`, `/profiles`, `/setup`, `/ops`
- более компактный header и уменьшенные hero-блоки без отдельной вкладки статуса
- favicon и базовая чистка адаптивной верстки таблиц
- автоматическая retention run history по дням с удалением старых локальных логов
- устойчивый `launchd` service-mode через service-копию вне `~/Documents`, чтобы macOS background auto-start не ломался на TCC/privacy ограничениях
- operational-скрипт `scripts/update_launchd_service.sh` для выката repo-кода в service-копию и controlled restart LaunchAgent
- operational-скрипт `scripts/launchd_status.sh` для проверки loaded/running state агента, PID, listener и хвоста service-логов
- additive миграция `sync_jobs` для безопасного добавления новых runtime-полей в уже существующую локальную SQLite-базу

### Чего пока нет

- двусторонняя синхронизация
- встроенная настройка `rclone remote` из интерфейса
- backup/restore без секретов
- macOS notifications и более полный migration flow service-копии / `launchd` на другой `Mac`
- optional relay-worker / VPS режим

## Операционные команды

### Локальный запуск

```bash
source .venv/bin/activate
uvicorn app.main:app
```

### Проверки

```bash
.venv/bin/python -m compileall app tests
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles tests.test_rclone tests.test_jobs tests.test_runtime_recovery
```

### Полезные `rclone` команды

```bash
rclone listremotes
rclone about yadisk:
rclone lsf "S3 Beget:bucket-name" --max-depth 1
```

### Полезные service-mode команды

```bash
./scripts/update_launchd_service.sh
./scripts/launchd_status.sh
```

## Хранение состояния

- БД приложения: `var/app.db`
- логи запусков: `var/logs/`
- runtime snapshots активных задач: `var/runtime/`
- временные ручные логи long-run переносов: сейчас использовался `/tmp/macbook-to-yadisk.log`

## Известные ограничения

- live progress сейчас работает для задач, запущенных из самого приложения
- при repeated retries truth source для “что осталось прямо сейчас” — live runtime snapshot текущего запуска; summary старых run нужен как исторический контекст, но не как финальный остаток
- restart-safe режим пока ограничен восстановлением последнего известного состояния и переводом run в `interrupted`
- автоматического продолжения уже стартовавшего `rclone` процесса после рестарта нет и не планируется: recovery идет через repeat-from-delta
- `S3 remote` в интерфейсе удобнее редактируется, но сам `rclone remote` все равно должен существовать заранее
- runtime tuning пока хранится только на уровне job; `profile defaults + job override` остаются следующим шагом product UX
- retry-логика завязана на временные сетевые ошибки и пока не выносится в UI-настройки
- часть UX еще рассчитана на ручную эксплуатацию, а не на долгоживущий production-like сервис
- перенос конфигурации между машинами и восстановление после reinstall пока не закрыты отдельным backup/restore UX

## Что логично делать дальше

1. Вернуться к backup/restore конфигурации без секретов как к главному эксплуатационному пробелу после `v2.1.1`.
2. Доработать операционный слой: notifications и migration flow service-mode на другой `Mac`.
3. Добавить `profile defaults` для runtime-параметров `rclone` поверх уже существующего `job override`.
4. После этого углубить diagnostics-to-ops mapping и guided setup для `rclone remote`.
5. Сохранять адресный operational polish без возврата к широкому visual redesign.
