# shelter-bot
Ботяра дискорда, который является хранителем нашего убежища

## Деплой на VPS

### Первоначальная настройка

1. Клонируйте репозиторий на VPS:
   ```bash
   git clone https://github.com/Tedeshi-Group/shelter-bot.git /opt/shelter-bot
   cd /opt/shelter-bot
   ```

2. Создайте виртуальное окружение и установите зависимости:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Создайте файл `.env` с переменными:
   ```
   DISCORD_TOKEN=ваш_токен
   GUILD_ID=ваш_guild_id
   DATABASE_URL=sqlite:///shelter_bot.db
   ```

4. Установите systemd сервис:
   ```bash
   sudo cp systemd/shelter-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable shelter-bot
   sudo systemctl start shelter-bot
   ```

### GitHub Secrets

Добавьте в Settings → Secrets → Actions:

| Secret | Описание |
|--------|----------|
| `VPS_HOST` | IP или домен VPS |
| `VPS_USER` | Пользователь SSH (например `bot`) |
| `VPS_SSH_KEY` | Приватный SSH ключ |
| `VPS_PATH` | Путь к проекту на VPS (например `/opt/shelter-bot`) |
