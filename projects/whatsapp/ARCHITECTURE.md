# WhatsApp Project — Deployment Architecture

## Split Architecture

This project runs as **two separate runtimes**. They are NOT co-located and must not be confused.

---

### 1. Local conversation handler (macOS LaunchAgent)

| Property | Value |
|---|---|
| Script | `projects/whatsapp/conversation_handler.py` (via `run-conversation.py`) |
| LaunchAgent | `com.paperclip.whatsapp-conversation` |
| Plist | `projects/whatsapp/com.paperclip.whatsapp-conversation.plist` |
| Host | Local macOS machine (`/Users/fabiodomingues/Desktop/Projects/paperclip/projects/whatsapp`) |
| Interval | Every 120 seconds |
| Logs | `~/.paperclip/logs/whatsapp-conversation-stdout.log` / `whatsapp-conversation-stderr.log` |

`conversation_handler.py` **never ships to production**. It runs only on the local machine via the LaunchAgent. Deploying it to the production server would require a separate, intentionally-designed deploy path (not currently implemented).

---

### 2. Production viewer app (Docker on VPS)

| Property | Value |
|---|---|
| Container | `whatsapp-viewer` |
| Port | `5050` (also reachable via `whatsapp.autohomeremodel.com`) |
| Production server | `64.226.74.167` (`/root/apps/whatsapp-viewer`) |
| Compose file | `projects/whatsapp/viewer/docker-compose.yml` |
| Deploy script | `projects/whatsapp/deploy-code.sh` (triggered by `com.paperclip.whatsapp-deploy`) |

The deploy script syncs **only** these files to the production server before rebuilding the container:

- `viewer/app.py`
- `viewer/requirements.txt`
- `db.py`
- `viewer/templates/index.html`
- `viewer/templates/dashboard.html`

`conversation_handler.py`, `run-conversation.py`, and all other local scripts are **explicitly excluded** from the deploy rsync.

---

## Key points for engineers

- **`whatsapp-viewer` (port 5050) is a read-only viewer.** It is not the runtime for `conversation_handler.py`.
- **All outbound WhatsApp logic runs locally**, via the `com.paperclip.whatsapp-conversation` LaunchAgent.
- The deploy path (`deploy-code.sh`) is viewer-only by design. Adding `conversation_handler.py` to the production server would require a new deploy target and a separate LaunchAgent or process manager on the server.
- See [WIN-352](/WIN/issues/WIN-352) and [WIN-354](/WIN/issues/WIN-354) for the investigation that confirmed this split.
