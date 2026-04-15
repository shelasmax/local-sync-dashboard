# Backlog

## P0

### 1. Backup / restore конфигурации приложения без секретов

Почему важно:
- это самый сильный эксплуатационный пробел после `v2.0.0`
- упростит перенос на другой `Mac` и восстановление локальной установки

Что должно быть:
- экспорт профилей и задач без секретов
- импорт с валидацией и понятными ошибками
- явное описание, что секреты остаются в `macOS Keychain`
- post-import flow: ручная перепроверка профилей и подсветка проблемных remote/path

### 2. Notifications + `launchd` + перенос на другой `Mac`

Почему важно:
- long-running sync уже используется как рабочий инструмент
- вручную мониторить завершение и сбои неудобно

Что должно быть:
- macOS notifications о `success`, `failed`, `interrupted`
- migration flow service-копии и launchd-конфигурации на другой `Mac`

### 3. Guided setup для `rclone remote`

Почему важно:
- это главный onboarding-разрыв после стабилизации основного UI
- cloud setup всё ещё разделён между UI и терминалом

Что должно быть:
- guided setup для `S3 remote`
- guided reconnect / OAuth guidance для `Yandex Disk`
- безопасная работа с Keychain и секретами
- явная связка `missing remote` → следующий setup-step

## P1

### 4. Дальнейшее углубление `Ops / Runbook` и diagnostics

Почему важно:
- база уже есть, но главный следующий выигрыш в точности corrective scenarios
- пользователь должен переходить от ошибки к действию ещё быстрее

Что должно быть:
- расширенное mapping ошибок профилей и запусков в конкретные сценарии `/ops`
- более плотная связка `последняя проверка` → `что проверить` → `что сделать дальше`
- richer scenarios для network/auth/mount/endpoint problems

### 5. `rclone` runtime-параметры как `profile defaults + job override`

Почему важно:
- на long-running маршрутах вроде `Synology SMB -> Yandex Disk` скорость часто определяется не только сетью, но и runtime-настройками `rclone`
- job-level runtime tuning уже есть в продукте, но пока это только часть решения
- часть настроек логичнее хранить как дефолты конкретного профиля / endpoint, а часть — как override у конкретного маршрута

Что должно быть:
- `profile defaults` для runtime-параметров `rclone`, которые разумно привязаны к конкретному хранилищу / endpoint
- уже существующий `job override` должен остаться и стать верхним слоем конфигурации для случаев, когда конкретный маршрут требует более агрессивной или более осторожной настройки
- явное правило приоритета: сначала override задачи, затем default профиля, затем базовые дефолты приложения
- безопасные рекомендованные значения и helper-текст для сценариев с большим числом маленьких файлов
- понятное разделение между безопасными дефолтами и более рискованными power-user настройками
- отображение effective `rclone` runtime flags в preview / run detail, чтобы пользователь понимал, с чем именно пошел запуск
- миграционный путь без поломки уже сохраненных job-level настроек

### 6. Адресный operational polish без редизайна

Почему важно:
- visual system уже стабилизирован
- дальше нужны только точечные улучшения, которые уменьшают время до действия

Что должно быть:
- калибровка counters, empty states и helper-блоков
- проверка, где icon-only action достаточен, а где нужен текст
- сохранение принципа: operational surfaces показывают текущее, глубокая история живёт в archive UX

## P2

### 7. Более явная диагностика mount path для Synology / SMB

Почему важно:
- ошибки вида `/home/...` vs `/Volumes/...` выглядят как сбой `rclone`, хотя корень проблемы в настройке профиля
- пользователю нужен более короткий путь от `directory not found` к правильному mount path на macOS

Что должно быть:
- action-oriented подсказки для `synology_share` и других mount-based профилей
- более явная связка между SMB URL из Finder и локальным путём в `/Volumes`
- отдельные UX-сценарии для случаев, когда mount path отвалился после sleep или переподключения сети

### 8. Более богатый operational runbook

Почему важно:
- базовый `/ops` уже есть, но пока это ещё не полноценный рабочий операционный контур

Что должно быть:
- richer runbook по типовым ошибкам
- onboarding для новых remotes
- более явный recovery/rollback раздел
- короткие сценарии “что делать дальше” для основных инцидентов

## Уже сделано

- релиз `v1.2.0`: orphan-run reaper (каждые 5 мин), SIGKILL grace period, широкая обработка исключений в `_execute_job`, безопасный I/O в stream-цикле
- релиз `v1.1.0`: `Control Room` с блоками `Status / Route / Next Action`
- edit/clone flow для задач прямо из `/jobs` и `/runs`
- browse API и быстрый выбор подпапок для формы задачи
- показ `effective route` в форме и на детальной странице запуска
- блокировка типовых ошибочных Synology маршрутов до запуска: `/home/...` и duplicated `source_path`
- incident-driven UX для repeated failed runs с явными CTA `Исправить`, `Копировать как новую`, `Dry-run`, `Открыть лог`
- принято продуктовое решение: `re-attach` к живому `rclone` процессу не поддерживается
- `dry-run preview` для сохраненной задачи перед реальным запуском
- recovery flow для `interrupted` задач с повтором по дельте из `/runs`, `/jobs`, карточки конкретного запуска и главной
- явный manual-start экран `/jobs/{id}/start` с checklist и CTA перед длинным cloud запуском
- cloud-to-cloud предупреждения в `/setup`, `/jobs`, `/runs` и run-level UX
- live progress, ETA и progress bar для активных app-managed задач
- runtime state активного run в памяти и в `var/runtime/`
- перевод незавершенных `running`-запусков в `interrupted` после рестарта приложения
- автоматическая retention run history по дням с удалением старых локальных логов
- удаление задач из UI и API с защитой от удаления активного job
- list-first UX для `/profiles` и `/jobs`: основной список и раскрывающаяся форма сверху
- единый icon-button action UX с `title` / tooltip и скрытым текстом для доступности
- упрощенный shell: более низкий header, более компактные hero-блоки и единый ритм карточек на `/`, `/jobs`, `/runs`, `/profiles`, `/setup`, `/ops`
- перенос operational readiness на дэшборд вместо отдельной вкладки статуса
- встроенный экран `/ops` с базовым operational runbook
- service-copy flow для `launchd` вне `~/Documents`, чтобы автоподъём на macOS работал без TCC-ошибок
- `scripts/update_launchd_service.sh` для синхронизации service-копии и controlled restart `launchd`-агента
- `scripts/launchd_status.sh` для проверки loaded/running state агента, listener на `127.0.0.1:8000` и хвоста service-логов
- job-level runtime tuning `rclone` в задаче: `transfers`, `checkers`, `fast-list`
- additive миграция `sync_jobs`, чтобы новые runtime-поля доезжали в существующую локальную БД без reset
- явные подсказки в `/profiles`, `/setup` и docs, что `Synology SMB` нужно заводить через локальный путь `/Volumes/...`, а не через `smb://...`
- helper-блоки в `/profiles`, ведущие из типовых diagnostic проблем в конкретные сценарии `/ops`
- counters на `/profiles` и более action-oriented profile helper UX
- стабильные empty-state и более честные operational sections на `/runs`, даже когда live snapshot сейчас отсутствует
- базовая чистка адаптивной верстки и cleanup тестового harness без `ResourceWarning`
- AJAX-поллинг `/api/runtime/jobs` вместо `location.reload()` на `/jobs`, `/runs`, `/run_detail`: устраняет блокировку UI при активных rclone-задачах
- TTL-кеш (30с) для `collect_setup_diagnostics()`: устраняет повторные `rclone listremotes` и сканирование `/Volumes` на каждый рендер страницы
- orphan-run reaper: периодическая (каждые 5 мин) проверка зависших `running`-запусков без живого потока, пометка как `interrupted`, чистка stale snapshot-файлов
- SIGKILL grace period в `_stream_process`: `SIGTERM` → 5 с ожидание → `SIGKILL` для процессов в D-state (stale SMB/NFS mount)
- широкая обработка исключений в `_execute_job`: неперехваченные исключения записывают `FAILED` в БД вместо forever-`running`
- безопасный I/O в stream-цикле: `readline()`/`read()` обёрнуты в `try/except`, selector cleanup в `finally`

## Отдельно вне MVP

- optional relay / VPS worker для long-running cloud маршрутов
- двусторонняя синхронизация
- delete propagation
- conflict resolution
- multi-user auth
- production-grade deployment за пределами локального Mac
