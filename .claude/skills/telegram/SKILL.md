---
name: telegram
description: >
  Check and send Telegram messages via @winnerdino_bot. Use when you need to
  read incoming Telegram messages from Fábio, send him a reply, or check bot
  health. Trigger on: "telegram check", "telegram send", "telegram status",
  "check telegram", "message Fábio", "reply to telegram".
user-invocable: true
---

# Telegram Skill

Direct Telegram access for the CEO. No intermediary agent needed.

## Configuration

- **Bot:** `@winnerdino_bot`
- **Chat ID:** `528866003` (Fábio, @WildCats99)
- **Token env file:** `/Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env`
- **Inbox file:** `/Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/data/inbox.jsonl`
- **State file:** `/Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/data/state.json`

Load the token before any Telegram API call:

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
```

## Commands

### telegram check

Read unread messages from the local inbox (populated by cron every 60s).

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
INBOX="/Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/data/inbox.jsonl"

if [ ! -f "$INBOX" ] || [ ! -s "$INBOX" ]; then
  echo "No messages in inbox."
  exit 0
fi

# Show recent messages (last 20)
tail -20 "$INBOX" | jq -r '"[\(.timestamp | todate)] \(.sender_name): \(.content)"'
```

If no arguments, show recent messages. To mark as read or get only unread, filter by `.read == "false"`.

For voice messages with `voice_file_id` set, you can transcribe on demand:

```bash
# Download and transcribe a voice message
FILE_ID="<voice_file_id from inbox>"
META=$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getFile?file_id=${FILE_ID}")
FILE_PATH=$(echo "$META" | jq -r '.result.file_path')
TMP=$(mktemp)
curl -fsS "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${FILE_PATH}" -o "$TMP"
# Transcribe with OpenAI Whisper if OPENAI_API_KEY is available
curl -fsS -X POST "https://api.openai.com/v1/audio/transcriptions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "file=@${TMP}" -F "model=gpt-4o-mini-transcribe" -F "response_format=json" | jq -r '.text'
rm "$TMP"
```

### telegram send

Send a message to Fábio via Telegram.

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
MESSAGE="Your message here"
curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${MESSAGE}"
```

Keep messages concise. Use plain text (Telegram supports basic markdown with `parse_mode=Markdown` if needed).

### telegram status

Check bot health and polling state.

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env

# Bot info
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | jq '.result | {username, first_name, is_bot}'

# Last poll state
STATE="/Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/data/state.json"
if [ -f "$STATE" ]; then
  echo "Last poll state:"
  cat "$STATE" | jq .
else
  echo "No polling state found — cron may not be running."
fi

# Inbox stats
INBOX="/Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/data/inbox.jsonl"
if [ -f "$INBOX" ]; then
  echo "Inbox: $(wc -l < "$INBOX" | xargs) messages total"
else
  echo "Inbox empty."
fi
```

### telegram poll

Force an immediate poll (useful if cron hasn't run yet or you want fresh messages).

```bash
bash /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/cron-poll.sh
```

## Notes

- The cron job runs `cron-poll.sh` every 60s to ingest messages from Telegram into the local inbox.
- Outbound messages go directly via Bot API — no intermediary needed.
- Voice transcription requires `OPENAI_API_KEY` in the environment.
- The inbox is append-only JSONL. Each line is a JSON object with: `sender_name`, `sender_id`, `timestamp`, `type`, `content`, `voice_file_id`, `read`.
