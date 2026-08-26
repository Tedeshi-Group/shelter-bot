---
feature: voice-recording
status: designed
updated: 2026-07-04
branch: feature/voice-recording
commits: 
---

# Voice Recording Orchestra

## Report

## [S1] Problem

Участники Discord-сервера ведут голосовые обсуждения, но не имеют возможности сохранить и прослушать эти разговоры позже. Текущая система архивирует только текстовые сообщения из войсов, но не само аудио. Требуется система из нескольких ботов для одновременной записи нескольких голосовых каналов.

## [S2] Design

### Архитектура оркестра

Система состоит из **3-5 ботов** (воркеров), управляемых **мастер-ботом** (основной бот shelter-bot).

```
┌─────────────────────────────────────────────────────────┐
│                    Мастер-бот (shelter-bot)              │
│  - Управляет пулом воркеров                             │
│  - Распределяет каналы (round-robin)                    │
│  - Управляет очередью                                   │
│  - Координирует загрузку на S3                          │
└───────────────────────┬─────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        │               │               │
        ▼               ▼               ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│  Воркер-бот 1 │ │  Воркер-бот 2 │ │  Воркер-бот 3 │
│  (recorder)   │ │  (recorder)   │ │  (recorder)   │
│  - Записывает │ │  - Записывает │ │  - Записывает │
│  - Загружает  │ │  - Загружает  │ │  - Загружает  │
└───────────────┘ └───────────────┘ └───────────────┘
```

### Компоненты

#### 1. BotManager (мастер)

Управляет пулом воркеров и очередью:

```python
class BotManager:
    workers: list[WorkerBot]          # Пул воркеров (3-5)
    queue: asyncio.Queue[VoiceChannel] # Очередь каналов
    current_index: int                 # Round-robin индекс
    
    async def assign_channel(channel: VoiceChannel) -> WorkerBot
    async def release_worker(worker: WorkerBot)
    async def process_queue()
```

#### 2. WorkerBot (воркер)

Один бот, который может записывать один канал:

```python
class WorkerBot:
    bot: commands.Bot
    token: str
    is_busy: bool
    current_channel: VoiceChannel | None
    current_recording: Recording | None
    
    async def start_recording(channel: VoiceChannel)
    async def stop_recording()
    async def upload_to_s3(audio_path: Path, metadata: dict)
```

#### 3. Recording (запись)

Модель одной записи:

```python
class Recording:
    id: int
    channel_id: int
    channel_name: str
    worker_bot_id: int
    started_at: datetime
    ended_at: datetime | None
    audio_path: Path          # Локальный путь
    s3_key: str | None        # S3 ключ после загрузки
    status: RecordingStatus   # recording | uploading | completed | failed
    participants: list[Participant]
```

### Триггеры записи

| Триггер | Описание |
|---------|----------|
| Автоматический | При создании нового голосового канала → мастер назначает свободного воркера |
| Команда `/record start` | Ручной старт записи (если есть свободный воркер) |
| Команда `/record stop` | Остановка записи конкретного канала |

### Round-robin алгоритм

```python
async def assign_channel(channel: VoiceChannel) -> WorkerBot | None:
    # 1. Проверяем свободных воркеров
    free_workers = [w for w in self.workers if not w.is_busy]
    
    if free_workers:
        # 2. Выбираем следующего по round-robin
        worker = free_workers[self.current_index % len(free_workers)]
        self.current_index += 1
        await worker.start_recording(channel)
        return worker
    
    # 3. Все заняты → добавляем в очередь
    await self.queue.put(channel)
    return None
```

### Очередь

Когда все воркеры заняты:

1. Канал добавляется в `asyncio.Queue`
2. Когда воркер освобождается → берёт следующий канал из очереди
3. Максимальный размер очереди: 10 каналов
4. Таймаут в очереди: 30 минут (если канал удалён до записи)

### Формат аудио

- **Кодек**: Opus (нативный для Discord)
- **Контейнер**: OGG
- **Частота дискретизации**: 48 kHz
- **Битрейт**: 64 kbps

### Хранилище S3

**Структура bucket:**
```
shelter-bot-voice/
├── 2026/
│   ├── 07/
│   │   ├── 04/
│   │   │   ├── worker-1_voice-1_20260704_143022.ogg
│   │   │   ├── worker-1_voice-1_20260704_143022_meta.json
│   │   │   └── ...
```

**Метаданные (JSON):**
```json
{
  "recording_id": 42,
  "channel_id": 123456789,
  "channel_name": "голосовой #1",
  "worker_bot_id": 1,
  "started_at": "2026-07-04T14:30:22Z",
  "ended_at": "2026-07-04T15:45:10Z",
  "duration_seconds": 4488,
  "participants": [
    {"discord_id": 111, "username": "user1", "duration_seconds": 3600},
    {"discord_id": 222, "username": "user2", "duration_seconds": 1800}
  ],
  "s3_key": "2026/07/04/worker-1_voice-1_20260704_143022.ogg",
  "file_size_bytes": 3584000
}
```

### Модель данных

**Таблица `voice_recordings`:**
```sql
CREATE TABLE voice_recordings (
    id INTEGER PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    channel_name VARCHAR(255),
    worker_bot_id INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    duration_seconds INTEGER,
    s3_key VARCHAR(512),
    file_size_bytes BIGINT,
    status VARCHAR(20) DEFAULT 'recording',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE voice_recording_participants (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER REFERENCES voice_recordings(id),
    user_discord_id BIGINT NOT NULL,
    username VARCHAR(255),
    joined_at TIMESTAMP,
    left_at TIMESTAMP,
    duration_seconds INTEGER
);

CREATE TABLE voice_recording_queue (
    id INTEGER PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    channel_name VARCHAR(255),
    queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) DEFAULT 'waiting',  -- waiting | assigned | expired
    assigned_worker_id INTEGER,
    expires_at TIMESTAMP
);
```

### Конфигурация ботов

**Переменные окружения:**
```env
# Мастер-бот (основной)
DISCORD_TOKEN=xxx

# Воркер-боты (3-5 токенов)
VOICE_WORKER_TOKEN_1=xxx
VOICE_WORKER_TOKEN_2=xxx
VOICE_WORKER_TOKEN_3=xxx
VOICE_WORKER_TOKEN_4=xxx  # опционально
VOICE_WORKER_TOKEN_5=xxx  # опционально

# S3 (Reg.ru)
S3_ENDPOINT=https://s3.regru.cloud
S3_ACCESS_KEY=D4VUQ86WLL99THBX1ZRS
S3_SECRET_KEY=0x8KqJepbtPHblRTDrpMNvUj0m5Gr2vluLBLmSpr
S3_BUCKET=shelter-bot-voice
```

### Команды

| Команда | Описание | Права |
|---------|----------|-------|
| `/record start` | Начать запись (если есть свободный воркер) | Администратор |
| `/record stop` | Остановить запись канала | Администратор |
| `/record status` | Статус всех воркеров и очереди | Все |
| `/record list [page]` | Список записей с пагинацией | Все |
| `/record play <id>` | Получить ссылку на запись | Все |
| `/record delete <id>` | Удалить запись | Администратор |
| `/record queue` | Показать очередь каналов | Администратор |

### Интеграция с существующей системой

- Мастер-бот использует существующий `VoiceChannels` cog
- При создании канала → `BotManager.assign_channel()`
- При удалении канала → остановка записи, загрузка на S3
- Логирование в `_voice_threads` (существующая система)

### Обработка ошибок

| Ситуация | Действие |
|----------|----------|
| Воркер не может подключиться | Пометить как failed, попробовать следующего |
| Воркер отключился во время записи | Переподключить или заменить другим |
| Ошибка S3 | Retry 3 раза, затем сохранить локально |
| Очередь переполнена | Уведомить админа, отклонить новые каналы |
| Все воркеры упали | Уведомить админа,暂停 запись |

### Мониторинг

**Статус воркеров:**
```
📊 Статус оркестра записи:
├── Воркер 1: 🟢 записывает "голосовой #1" (45:22)
├── Воркер 2: 🟢 записывает "голосовой #3" (12:05)
├── Воркер 3: 🔴 ошибка (переподключение...)
├── Воркер 4: ⚪ свободен
└── Воркер 5: ⚪ свободен

📋 Очередь: 2 канала ожидают
```

## [S3] Out of Scope

- Запись экрана (требует отдельного клиента)
- Транскрипция голоса в текст
- Распознавание говорящих (speaker diarization)
- Веб-интерфейс для прослушивания
- Автоматическое масштабирование количества воркеров

## Tasks

- [ ] T1: Добавить зависимости (boto3, PyNaCl) — acceptance: requirements.txt обновлён (covers: S2)
- [ ] T2: Создать модели данных (voice_recordings, participants, queue) — acceptance: миграция Alembic создана (covers: S2)
- [ ] T3: Реализовать WorkerBot — acceptance: воркер подключается к каналу и записывает аудио (covers: S2)
- [ ] T4: Реализовать BotManager с round-robin — acceptance: менеджер распределяет каналы между воркерами (covers: S2)
- [ ] T5: Реализовать очередь каналов — acceptance: каналы ставятся в очередь при занятых воркерах (covers: S2)
- [ ] T6: Интеграция с S3 — acceptance: записи загружаются на S3, метаданные сохраняются (covers: S2)
- [ ] T7: Реализовать команды /record — acceptance: команды работают корректно (covers: S2)
- [ ] T8: Интеграция с VoiceChannels cog — acceptance: автоматическая запись при создании каналов (covers: S2)
- [ ] T9: Мониторинг и статус воркеров — acceptance: /record status показывает состояние оркестра (covers: S2)
- [ ] T10: Обработка ошибок и retry — acceptance: ошибки обрабатываются gracefully (covers: S2)
- [ ] T11: Тестирование на VPS — acceptance: оркестр работает с 3+ ботами (covers: S2)
