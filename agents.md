# agents.md

## Purpose

Этот файл фиксирует локальные правила для агентной работы именно в `local-sync-dashboard`.

## Product Boundaries

- Продукт локальный, ориентирован на `macOS`.
- Основа переноса данных — `rclone`, а не собственные cloud SDK.
- Текущий MVP поддерживает только однонаправленный `copy`.
- Автоматические удаления и двусторонняя синхронизация запрещены по умолчанию.

## High-Risk Areas

Особая осторожность нужна при изменениях в:

- `app/services/profiles.py` — секреты, Keychain, profile lifecycle
- `app/services/jobs.py` — scheduler, фоновые потоки, retry и cancel
- `app/services/rclone.py` — фактическое поведение переноса
- `launchd/com.localsync.dashboard.plist` — автозапуск и локальная эксплуатация

Для таких изменений всегда указывать:

- риск
- impact area
- rollback
- ручную проверку

## Repo Conventions

- Не заменять `rclone copy` на `sync` без явного запроса.
- Не добавлять destructive behavior в job execution.
- Не хранить секреты в SQLite или в логах.
- Для S3/Yandex профилей помнить: UI удобнее, но `rclone remote` должен существовать заранее.
- Помнить, что cloud-to-cloud сценарии идут через локальный `Mac`, а не напрямую между провайдерами.
- Для long-running job учитывать два слоя состояния: финальный `RunHistory` и runtime snapshots в `var/runtime/`.
- Предпочитать маленькие обратимые diff.

## Important Files

- `app/main.py` — HTTP/UI слой
- `app/models.py` — схема хранения состояния
- `app/services/profiles.py` — профили и проверки доступности
- `app/services/jobs.py` — runtime и scheduler
- `app/services/rclone.py` — команды `rclone`
- `app/templates/*.html` — UI
- `app/static/style.css` — стили
- `app/static/favicon.svg` — favicon
- `var/runtime/` — snapshots активных задач для restart-safe recovery
- `tests/` — текущая unit-проверка

## Commands

### Run app

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

### Tests

```bash
.venv/bin/python -m compileall app tests
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles tests.test_rclone tests.test_jobs tests.test_runtime_recovery
```

### Useful ops

```bash
rclone listremotes
rclone about yadisk:
rclone lsf "S3 Beget:bucket-name" --max-depth 1
```

## Current Priorities

1. Решение по re-attach к живому `rclone` процессу после рестарта
2. Улучшение UX и диагностики ошибок профилей
3. Более явные cloud-to-cloud подсказки про локальный путь трафика на уровне run и запуска
4. Операционная документация: recovery, launchd и backup/restore

Примечание:
- базовая ручная перепроверка профилей и hints уже есть; следующий шаг в этой зоне — сохранять последнюю ошибку проверки и делать диагностику еще богаче.
- базовые cloud-to-cloud предупреждения уже есть в `/setup` и `/jobs`; дальше нужен более явный UX рядом с запуском и историей run.

## Definition of Done

Изменение считается завершенным, если:

- есть минимальная проверка или тест
- UI не ломается на мобильной ширине
- нет утечки секретов
- новая логика не делает удалений в источнике или назначении
- если затронут runtime долгих задач, обновлены docs и учтен сценарий restart/recovery
- в финальном ответе есть краткая проверка и оставшиеся риски
