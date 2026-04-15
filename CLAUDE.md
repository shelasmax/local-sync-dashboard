# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**Local Sync Dashboard** — локальная macOS-утилита с веб-интерфейсом для управления однонаправленными заданиями копирования данных между хранилищами (локальные папки, Synology, S3, Yandex Disk) через `rclone`.

Ключевые ограничения по дизайну:
- Только `rclone copy` — никогда `rclone sync`. Автоматические удаления запрещены.
- Одно конкурентное задание одновременно.
- Секреты хранятся только в macOS Keychain, не в SQLite и не в логах.
- Трафик cloud-to-cloud всегда идёт через локальный Mac.
- Synology: только уже смонтированный путь `/Volumes/...`, не `smb://` или NAS-пути.
- Нет re-attach к живому rclone-процессу после рестарта — recovery через повторный copy с дельты.

## Commands

### Запуск приложения
```bash
source .venv/bin/activate
uvicorn app.main:app
# http://127.0.0.1:8000
```

Для launchd-managed service-copy на этом Mac:
```bash
./scripts/update_launchd_service.sh
./scripts/launchd_status.sh
```

### Установка зависимостей
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Тесты
```bash
# Все тесты
.venv/bin/python -m unittest discover tests

# Конкретные модули
.venv/bin/python -m unittest tests.test_profiles tests.test_jobs

# Проверка синтаксиса
.venv/bin/python -m compileall app tests
```

### Полный прогон (синтаксис + тесты)
```bash
.venv/bin/python -m compileall app tests && \
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles tests.test_rclone tests.test_jobs tests.test_runtime_recovery
```

### Полезные rclone-команды для отладки
```bash
rclone listremotes
rclone about yadisk:
rclone lsf "S3 Beget:bucket-name" --max-depth 1
```

## Architecture

### Stack
FastAPI (HTTP/API) + Jinja2 (HTML-шаблоны) + SQLAlchemy/SQLite (`var/app.db`) + APScheduler + rclone CLI + macOS Keychain.

### Слои
```
app/main.py           ← HTTP-маршруты и шаблоны (HTML + REST API /api/*)
    ↓
app/services/
  jobs.py             ← JobRunner: APScheduler + фоновые потоки + RunSnapshot
  profiles.py         ← CRUD профилей, тест соединения, Keychain, диагностика
  rclone.py           ← сборка команд, execute / execute_with_progress, парсинг ошибок
  system.py           ← обнаружение rclone remotes и локальных кандидатов
  keychain.py         ← обёртка над macOS `security` CLI
  scheduling.py       ← трансляция строк ("hourly", "cron:...") → APScheduler CronTrigger
  common.py           ← join_local/remote_path, JSON-утилиты, is_temporary_network_error
app/models.py         ← ORM: StorageProfile, SyncJob, RunHistory, AppSettings
app/schemas.py        ← Pydantic: валидация запросов/ответов
app/config.py         ← настройки (LOCAL_SYNC_DATA_DIR, var/, Keychain-префикс)
```

### Двухуровневое состояние долгих задач
- **`var/app.db`** — финальные записи `RunHistory` (статус, байты, файлы, лог-путь)
- **`var/runtime/{run_id}.json`** — `RunSnapshot` для активного задания: байты, скорость, ETA, active items. Обновляется каждые 1–2 с. Пережит рестарт uvicorn — незавершённые runs помечаются `interrupted`, доступны для retry.

### Жизненный цикл задания
1. `JobRunner.sync_job()` регистрирует APScheduler-триггер.
2. По расписанию или вручную: `enqueue_job()` → фоновый поток.
3. Перед запуском runtime-path при необходимости добирает additive-миграции `sync_jobs`, чтобы новые поля не ломали service rollout на существующей локальной БД.
4. `rclone.build_copy_command()` → `execute_with_progress()` стримит вывод.
5. `progress_callback` парсит JSON-stats, обновляет `RunSnapshot`.
6. Завершение → запись `RunHistory` в БД.

### Runtime tuning `rclone`
- На уровне job уже поддерживаются `rclone_transfers`, `rclone_checkers`, `rclone_fast_list`.
- Это предназначено в первую очередь для long-running маршрутов с большим числом маленьких файлов, например `Synology SMB -> Yandex Disk`.
- Без явной причины не поднимать parallelism агрессивно: сначала разумный уровень вроде `transfers=8`, `checkers=16`, затем только смотреть на эффект.
- Следующий продуктовый шаг в этой зоне — не новые ad-hoc флаги в коде, а `profile defaults + job override`.

### UI-конвенции (не ломать без причины)
- `/profiles` и `/jobs` — list-first UX: список всегда виден, форма создания открывается сверху через раскрывающийся блок.
- Action-кнопки — icon-only с `title`/tooltip. Не смешивать с длинным текстом рядом.

## High-Risk Areas

При изменениях в этих файлах всегда указывать: риск, зону влияния, rollback, ручную проверку:
- `app/services/profiles.py` — секреты, Keychain, lifecycle профиля
- `app/services/jobs.py` — scheduler, потоки, retry, cancel
- `app/services/rclone.py` — фактическое поведение копирования
- `app/database.py` — additive миграции SQLite и совместимость service rollout со старой схемой
- `launchd/com.localsync.dashboard.plist` — автозапуск и эксплуатация
- `scripts/update_launchd_service.sh` — выкладка repo-кода в service-копию и restart LaunchAgent
- `scripts/launchd_status.sh` — проверка loaded/running state агента, порта и service-логов

## Definition of Done

Изменение считается завершённым, если:
- есть тест или минимальная проверка
- UI не ломается на мобильной ширине
- нет утечки секретов
- новая логика не делает удалений в источнике или назначении
- если затронут runtime долгих задач — учтён сценарий restart/recovery
