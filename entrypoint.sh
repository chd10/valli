#!/bin/bash
set -e

# Export env vars in a format suitable for sourcing by cron jobs
env | grep -E '^(TG_BOT_TOKEN|ANTHROPIC_API_KEY|MANAGER_CHAT_ID|SMTP_|EMAIL_TO|YADISK_)' \
    | sed 's/^/export /' > /app/env.sh

cron
exec python3 bot.py
