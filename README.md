# IntelliCare Assessment Bot

Bilingual Telegram bot for the St. Urael Clinic implementation assessment.

## Run locally

Install dependencies with `pip install -r requirements.txt`, then set these environment variables in a local `.env` file:

- `BOT_TOKEN`: Telegram bot token
- `ADMIN_IDS`: optional comma-separated Telegram user IDs allowed to use admin commands
- `DB_PATH`: optional SQLite database path; defaults to `assessment.db`

Start the bot with `python Bot.py`.

## Deploy on Railway

Set `BOT_TOKEN` in the Railway service variables. Optionally set `ADMIN_IDS` and `DB_PATH`. Use `python Bot.py` as the service start command.