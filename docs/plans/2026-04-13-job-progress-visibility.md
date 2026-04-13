# Job Progress Visibility Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Показать в UI live progress активной задачи с progress bar, ETA, скоростью и остатком по файлам/байтам.

**Architecture:** Не менять базовый движок `rclone copy`, а добавить слой runtime telemetry поверх уже существующего `JobRunner`. Источник данных — JSON stats из `rclone`, которые нужно парсить и временно хранить для активных run, а затем отдавать в HTML/API.

**Tech Stack:** FastAPI, SQLAlchemy, Jinja2, APScheduler, `rclone --use-json-log`

---

### Task 1: Формализовать runtime state активного run

**Files:**
- Modify: `app/services/jobs.py`
- Test: `tests/test_profiles.py`

**Step 1: Добавить dataclass для live progress**

Добавить структуру с полями:
- `job_id`
- `run_id`
- `bytes_done`
- `bytes_total`
- `files_done`
- `files_total`
- `speed_bytes_per_sec`
- `eta_seconds`
- `current_items`
- `updated_at`

**Step 2: Подвесить state к `JobRunner`**

Добавить словарь по `job_id` или `run_id` для хранения live telemetry в памяти.

**Step 3: Обновлять state во время выполнения**

Парсить JSON-строки `rclone` и обновлять live progress без ожидания завершения всей команды.

**Step 4: Удалять state после завершения**

После `success`, `failed` или `canceled` убирать live state и оставлять финальные итоги в `RunHistory`.

**Step 5: Прогнать локальные тесты**

```bash
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles
```

### Task 2: Дать API для текущего прогресса

**Files:**
- Modify: `app/main.py`

**Step 1: Добавить endpoint live progress**

Добавить:
- `GET /api/jobs/{job_id}/progress`
- `GET /api/jobs/progress`

**Step 2: Нормализовать ответ API**

Возвращать:
- статус
- проценты
- ETA
- скорость
- transferred / total
- активные файлы

**Step 3: Обработать отсутствие live state**

Если задача не выполняется, возвращать:
- `running: false`
- последние известные итоговые метрики из `RunHistory`

### Task 3: Показать прогресс в UI задач

**Files:**
- Modify: `app/templates/jobs.html`
- Modify: `app/static/style.css`

**Step 1: Добавить блок progress bar в строку job**

Показать:
- цветной бар
- процент
- ETA
- скорость

**Step 2: Показать bytes/files**

Показать:
- `X GiB из Y GiB`
- `N файлов из M`

**Step 3: Показать текущие активные файлы**

Сделать короткий список из 2-4 items для понимания, что реально копируется.

### Task 4: Показать прогресс в UI запусков

**Files:**
- Modify: `app/templates/runs.html`
- Modify: `app/templates/run_detail.html`

**Step 1: На списке запусков**

Если run активен, показывать live metrics вместо пустого summary.

**Step 2: На детальной странице run**

Показать:
- progress bar
- ETA
- speed
- список текущих файлов
- timestamp последнего обновления

### Task 5: Добавить клиентское автообновление

**Files:**
- Modify: `app/templates/jobs.html`
- Modify: `app/templates/run_detail.html`

**Step 1: Самый простой polling**

Каждые 5-10 секунд дергать API и обновлять DOM.

**Step 2: Не делать лишнего**

Не внедрять WebSocket в MVP. Polling достаточно.

**Step 3: Обрабатывать ошибки сети**

Если polling упал, показывать “нет свежих данных”, но не ломать страницу.

### Task 6: Зафиксировать поведение в docs

**Files:**
- Modify: `README.md`
- Modify: `docs/PROJECT.md`
- Modify: `docs/BACKLOG.md`

**Step 1: Описать новый runtime progress**

Добавить краткое описание live progress и ограничений.

**Step 2: Обновить backlog**

Снять этот пункт из `P0`, если реализация завершена.

### Task 7: Финальная проверка

**Files:**
- Modify: `tests/test_common.py`
- Create: `tests/test_jobs_progress.py`

**Step 1: Добавить unit-тест на парсинг прогресса**

Проверить разбор JSON stats из `rclone`.

**Step 2: Добавить тест на API progress**

Проверить, что endpoint возвращает ожидаемую структуру для активной и неактивной задачи.

**Step 3: Прогнать проверки**

```bash
.venv/bin/python -m compileall app tests
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles
```

**Step 4: Commit**

```bash
git add README.md docs/PROJECT.md docs/BACKLOG.md docs/plans/2026-04-13-job-progress-visibility.md app/main.py app/services/jobs.py app/templates/jobs.html app/templates/runs.html app/templates/run_detail.html app/static/style.css tests
git commit -m "feat: add live job progress visibility"
```
