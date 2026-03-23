# Shared Tools

These tools are available to all Paperclip agents. This is the canonical reference — do not duplicate tool docs in agent-specific files. Read the relevant section before using a tool.

## Telegram

Direct access to @winnerdino_bot for messaging Fábio (chat_id 528866003, @WildCats99).

### Setup

- **Token**: env file at `projects/telegram/.env` (contains `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`)
- **Inbox**: `projects/telegram/data/inbox.jsonl` — append-only JSONL, populated by cron every 60s
- **State**: `projects/telegram/data/state.json` — polling offset tracker
- **Cron**: system crontab runs `projects/telegram/cron-poll.sh` every 60s

Always load the env before any Telegram API call:

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
```

### Check messages

Read recent inbound messages from the local inbox:

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
INBOX="/Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/data/inbox.jsonl"
if [ -f "$INBOX" ] && [ -s "$INBOX" ]; then
  tail -20 "$INBOX" | jq -r '"[\(.timestamp | todate)] \(.sender_name): \(.content)"'
else
  echo "No messages in inbox."
fi
```

Each inbox line is JSON: `{sender_name, sender_id, timestamp, type, content, voice_file_id, read}`.

### Send a message

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=YOUR MESSAGE HERE"
```

### Force immediate poll

```bash
bash /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/cron-poll.sh
```

### Voice transcription (requires OPENAI_API_KEY)

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
FILE_ID="<voice_file_id from inbox>"
META=$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getFile?file_id=${FILE_ID}")
FILE_PATH=$(echo "$META" | jq -r '.result.file_path')
TMP=$(mktemp)
curl -fsS "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${FILE_PATH}" -o "$TMP"
curl -fsS -X POST "https://api.openai.com/v1/audio/transcriptions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "file=@${TMP}" -F "model=gpt-4o-mini-transcribe" -F "response_format=json" | jq -r '.text'
rm "$TMP"
```

## Google Docs

Create, read, edit Google Docs in the shared Paperclip Drive folder. Uses OAuth2 user credentials.

- **Script**: `projects/google-docs/gdocs.py`
- **OAuth token**: `projects/google-docs/data/oauth_token.json` (auto-refreshes)
- **Shared folder**: `1pXbU19XxvZfq1QbY3bvBZtOiQ7J8G36C`

### Usage

```bash
# Create a new doc
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py create "Title" "Initial content"

# Read a doc
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py read <doc_id>

# Append content to a doc
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py append <doc_id> "More content"

# Replace all content in a doc
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py replace <doc_id> "New content"

# List docs in shared folder
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py list
```

### Auth test

```bash
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py auth-test
```
