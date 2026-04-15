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
- Для `synology_share` всегда использовать уже смонтированный локальный путь macOS из `/Volumes`, а не `smb://...` и не NAS-путь вроде `/home`.
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

1. Экспорт и backup/restore конфигурации приложения без секретов
2. Связка диагностики профилей с `Ops / Runbook` и более глубокие action-oriented подсказки
3. Операционный UX: нотификации, `launchd`-сценарии и перенос на другой `Mac`
4. Guided setup для `rclone remote`
5. Дальнейшая адресная калибровка UI-copy и operational helper-блоков без широкого редизайна

Примечание:
- релиз `v1.1.0` уже зафиксировал `Control Room`, list-first экраны и более компактный shell; следующие правки должны быть адресными, а не широким редизайном.
- встроенный экран `/ops` уже есть; следующие шаги в этой зоне — превращать runbook из справки в рабочие UX-сценарии.
- базовая ручная перепроверка профилей, hints, сохранение последнего результата проверки, richer hints по типам ошибок и action-oriented CTA уже есть; следующий шаг в этой зоне — углублять диагностику еще дальше и связывать ошибки с `Ops / Runbook`.
- для Synology SMB всегда помнить продуктовую интерпретацию: Finder URL `smb://host/share/path` в приложении должен превращаться в локальный mount path `/Volumes/share/path` или `/Volumes/share` + `source_path`.
- базовые cloud-to-cloud предупреждения уже есть в `/setup`, `/jobs`, `/runs` и карточке конкретного запуска; дальше нужен еще более явный UX перед длинными ночными стартами.
- страницы `/profiles` и `/jobs` уже переведены в list-first UX: список остается основным экраном, а создание новой сущности открывается через раскрывающийся блок сверху; не возвращать split-view без явной причины.
- action UX теперь смешанный: основные CTA текстовые, вторичные destructive/service actions могут оставаться compact/icon-only там, где контекст очевиден.
- `re-attach` к живому `rclone` процессу после рестарта не делаем; recovery-модель проекта — повторный `copy` с дельты.

## Definition of Done

Изменение считается завершенным, если:

- есть минимальная проверка или тест
- UI не ломается на мобильной ширине
- нет утечки секретов
- новая логика не делает удалений в источнике или назначении
- если затронут runtime долгих задач, обновлены docs и учтен сценарий restart/recovery
- в финальном ответе есть краткая проверка и оставшиеся риски
