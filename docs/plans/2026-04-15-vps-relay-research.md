# VPS / Relay Research Note

## Why this exists

`local-sync-dashboard` в `v1.1` по-прежнему выполняет все cloud маршруты через локальный `Mac`. Для больших ночных `S3 -> Yandex` и похожих сценариев это ограничение по uptime и стабильности, поэтому вариант с always-on relay-host или VPS имеет смысл рассматривать отдельно от MVP.

## What this is not

- не часть текущего релиза
- не переход на server-side copy между провайдерами
- не разрешение на хранить секреты где попало

Даже на VPS это всё ещё будет relay-flow: удалённый `rclone` будет читать source и затем отправлять данные в target.

## Options to compare

### 1. Current local Mac relay

Плюсы:
- минимальная архитектура
- секреты уже живут локально
- самый лёгкий rollback

Минусы:
- sleep / reboot / нестабильный интернет
- долгие маршруты требуют ручной operational discipline

### 2. Always-on Mac mini / другой dedicated Mac

Плюсы:
- почти тот же продуктовый контур
- меньше изменений в архитектуре
- лучше uptime для ночных задач

Минусы:
- всё ещё локальная single-user модель
- нужна более строгая `launchd` и backup/restore история

### 3. Optional VPS relay worker

Плюсы:
- выше uptime
- пользовательский Mac можно не держать постоянно активным

Минусы:
- нужен новый контур для секретов и `rclone remotes`
- появится удалённый runtime/log state
- нужно продумать стоимость egress и трафика
- возрастает blast radius и security surface

## Questions to answer before any implementation

1. Где должны храниться `rclone remotes` и как переносить их между UI и worker?
2. Какой будет источник правды для runtime state и `RunHistory`: локальный SQLite, worker-side storage или гибрид?
3. Как показывать логи и progress из удалённого worker без ломки текущего UI?
4. Есть ли реальный выигрыш по скорости, или эффект будет только в uptime?
5. Сколько будет стоить egress и двусторонний трафик для типовых объёмов?

## Recommendation

Не идти в VPS-реализацию, пока не будут закрыты:

- backup/restore конфигурации без секретов
- notifications + `launchd` operational flow
- более зрелая модель переноса на другой `Mac`

После этого разумнее сначала проверить вариант с `always-on Mac`, и только если он не закрывает сценарии long-running cloud jobs, переходить к отдельному epic про optional relay-worker / VPS.
