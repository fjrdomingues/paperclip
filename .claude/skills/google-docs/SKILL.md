---
name: google-docs
description: >
  Create, read, edit, and list Google Docs in the shared Paperclip Drive folder.
  Use when you need to produce a document as output, collaborate on long-form
  content, or store structured reports outside of issue comments.
  Trigger on: "google docs", "create doc", "write doc", "gdocs", "google document".
user-invocable: true
---

# Google Docs Skill

Create and manage Google Docs in the shared Paperclip Drive folder via a service account.

## Configuration

- **Service account**: `paperclip-drive-access@code-autopilot.iam.gserviceaccount.com`
- **Shared folder ID**: `1pXbU19XxvZfq1QbY3bvBZtOiQ7J8G36C`
- **Credentials**: `/Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/credentials.json`
- **Script**: `/Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py`
- **Token cache**: `/Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/data/token_cache.json`

## Commands

All commands use the helper script. No external dependencies — uses Python stdlib + openssl.

### Create a document

```bash
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py create "Document Title" "Optional initial content"
```

Returns JSON with `id`, `title`, and `url`.

### Read a document

```bash
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py read <doc_id>
```

Returns JSON with `id`, `title`, and `content` (plain text).

### Append to a document

```bash
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py append <doc_id> "Content to append"
```

### Replace document content

```bash
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py replace <doc_id> "New full content"
```

### List documents in shared folder

```bash
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py list [max_results]
```

### Test authentication

```bash
python3 /Users/fabiodomingues/Desktop/Projects/paperclip/projects/google-docs/gdocs.py auth-test
```

## Notes

- Auth uses a service account JWT signed with openssl. Tokens are cached for 1 hour.
- All docs are created in the shared folder. The service account cannot access anything outside that folder.
- The script uses only Python stdlib (no pip packages required).
- For long content, pipe via stdin or write to a temp file and use `$(cat file.txt)` as the content arg.
