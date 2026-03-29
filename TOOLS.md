# Shared Tools

These tools are available to all Paperclip agents. This is the canonical reference — do not duplicate tool docs in agent-specific files. Read the relevant section before using a tool.

## Prerequisites / Agent Tooling

These CLI tools must be installed on the machine running agent heartbeats. Install once; they persist across sessions.

### qmd

`qmd` is used by the `para-memory-files` skill for semantic recall queries. Install globally via npm:

```bash
npm install -g @tobilu/qmd
```

**Verified working on:** macOS with nvm-managed Node.js. `qmd` lands in the active nvm Node version's bin directory (e.g. `~/.nvm/versions/node/v25.8.1/bin/`).

**Supported shell conditions:**

| Shell type | Works? | Why |
|---|---|---|
| Inherited shell (Claude Code / agent adapters) | ✅ | Inherits parent env; nvm already active |
| Fresh interactive shell (`zsh -i`) | ✅ | `.zshrc` sources nvm; nvm default must be the version with qmd installed |
| Fresh login shell (`zsh -l`) | ❌ | `.zshrc` is skipped; nvm never loads |

**Requirements:**
1. Install `qmd` under the nvm version you want as the default, then set it as the nvm default:
   ```bash
   nvm use 25.8.1
   npm install -g @tobilu/qmd
   nvm alias default 25.8.1
   ```
2. Confirm `~/.zshrc` sources nvm (standard nvm install adds this automatically).
3. Login shells (`zsh -l`, `zsh --login`) are **not supported** without also adding nvm init to `~/.zprofile`.

**Verify:**
```bash
# Inherited / interactive shell
which qmd        # → ~/.nvm/versions/node/vX.Y.Z/bin/qmd
qmd --version    # → qmd 2.0.1 (bab86d5)

# Confirm fresh interactive shell works
env -i HOME="$HOME" /bin/zsh -ic 'which qmd; qmd --version'

# Fresh login shell — expected to fail unless nvm is in ~/.zprofile
env -i HOME="$HOME" /bin/zsh -lc 'which qmd'
```

**To enable login shell support** (optional — requires editing `~/.zprofile`):
```bash
# Add to ~/.zprofile:
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
```

## Telegram

Direct access to @winnerdino_bot for messaging Fábio (chat_id 528866003, @WildCats99).

### Setup

- **Token**: env file at `projects/telegram/.env` (contains `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`)
- **Inbox**: `projects/telegram/data/inbox.jsonl` — append-only JSONL, populated every 60s
- **State**: `projects/telegram/data/state.json` — polling offset and auto-wake cooldown tracker
- **Poller**: launchd agent `com.paperclip.telegram-poll` runs `projects/telegram/cron-poll.sh` every 60s (survives macOS sleep, catches up on wake)

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

Each inbox line is JSON: `{sender_name, sender_id, timestamp, type, content, voice_file_id, document_file_id, photo_file_id, read}`.

### Send a message

**Important — single-point communication rule (per org):**

This rule applies differently depending on the organisation:

- **Multi-agent orgs** (e.g. the main Paperclip company with a CEO, CTO, Chief of Staff, etc.): only the designated Head/Chief of Staff sends Telegram messages to Fábio. All other agents report via Paperclip issue comments; the Head consolidates and relays. Exception: only when the Head explicitly delegates a direct reply to a specific agent.
- **Single-agent orgs** (e.g. the fitness/gym org with only the Head Personal Trainer): the sole agent communicates directly with Fábio via Telegram — no relay needed.

In all cases:
- Always start your message with `[Your Name or Role]` so Fábio knows who is talking (e.g. `PT: ...` for the Personal Trainer).
- Reason: multiple agents replying about the same topic floods Fábio's Telegram and wastes his time.

```bash
bash /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/send.sh "YOUR MESSAGE HERE" "YourRole"
```

This wrapper sends via the Telegram API and logs the outbound message to `inbox.jsonl` (with `direction: "outbound"`) so agents have full conversation context.

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

### Image reading (requires OPENAI_API_KEY)

Download a photo from Telegram and describe it using OpenAI Vision:

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
FILE_ID="<photo_file_id from inbox>"
META=$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getFile?file_id=${FILE_ID}")
FILE_PATH=$(echo "$META" | jq -r '.result.file_path')
TMP=$(mktemp /tmp/photo_XXXXXX.jpg)
curl -fsS "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${FILE_PATH}" -o "$TMP"
# Read the image using the Read tool (Claude is multimodal) or send to OpenAI Vision:
BASE64=$(base64 < "$TMP")
curl -fsS -X POST "https://api.openai.com/v1/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"gpt-4o-mini\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"Describe this image in detail.\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/jpeg;base64,${BASE64}\"}}]}],\"max_tokens\":500}" | jq -r '.choices[0].message.content'
rm "$TMP"
```

### Document download

Download a document from Telegram by file_id:

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
FILE_ID="<document_file_id from inbox>"
bash -c 'source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/cron-poll.sh; download_telegram_document "'"$FILE_ID"'" "filename.pdf"'
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
