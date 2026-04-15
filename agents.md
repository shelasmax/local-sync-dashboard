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
- Для списков запусков и задач придерживаться принципа: operational surfaces показывают текущее и требующее действия состояние, глубокая история живет в archive UX.
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
uvicorn app.main:app
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
./scripts/update_launchd_service.sh
./scripts/launchd_status.sh
```

## Current Priorities

1. Экспорт и backup/restore конфигурации приложения без секретов
2. Операционный UX: нотификации и migration flow service-mode на другой `Mac`
3. Guided setup для `rclone remote`
4. Связка диагностики профилей с `Ops / Runbook` и более глубокие action-oriented подсказки
5. Дальнейшая адресная калибровка operational helper-блоков и incident UX без широкого редизайна

Примечание:
- релиз `v1.1.0` уже зафиксировал `Control Room`, list-first экраны и более компактный shell; следующие правки должны быть адресными, а не широким редизайном.
- релиз `v1.2.1` уже зафиксировал archive UX для запусков: отдельный `/runs/archive`, pagination, ручную cleanup-очистку локальной истории, компактные recovery-блоки и persistent `Скрыть` / `Показать`; следующий шаг в этой зоне — не расширять историю обратно в operational surfaces.
- релиз `v1.3.0` уже зафиксировал прямую связку profile diagnostics → `/ops`, counters на `/profiles` и более устойчивый `/runs` с empty-state вместо пропадающих operational секций.
- релиз `v2.0.0` уже зафиксировал service-copy model для `launchd`, канонический порт `8000` и operational-скрипты для update/status; следующий шаг в этой зоне — не возвращаться к запуску service-mode напрямую из `~/Documents`, а закрывать notifications и migration flow.
- верхний незакрытый продуктовый приоритет теперь эксплуатационный: переносимость конфигурации, service-mode и migration flow, а не новый visual layer.
- встроенный экран `/ops` уже есть; следующие шаги в этой зоне — углублять corrective scenarios, а не строить второй параллельный troubleshooting UX.
- базовая ручная перепроверка профилей, hints, сохранение последнего результата проверки, richer hints по типам ошибок и action-oriented CTA уже есть; прямые переходы из диагностики в сценарии `/ops` тоже уже есть. Следующий шаг — расширять покрытие сценариев после закрытия backup/restore и service-mode.
- автоматическая retention run history по дням уже есть вместе с ручной cleanup-очисткой локальной истории; дальнейшие правки в lifecycle должны сохранять разделение между operational `/runs` и archive UX.
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
