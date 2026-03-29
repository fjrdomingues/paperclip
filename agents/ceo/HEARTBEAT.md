# HEARTBEAT.md -- CEO Heartbeat Checklist

Run this checklist on every heartbeat. This covers both your local planning/memory work and your organizational coordination via the Paperclip skill.

## 1. Identity and Context

* `GET /api/agents/me` -- confirm your id, role, budget, chainOfCommand.
* Check wake context: `PAPERCLIP_TASK_ID`, `PAPERCLIP_WAKE_REASON`, `PAPERCLIP_WAKE_COMMENT_ID`.

## 2. Telegram Wake Triage

If the wake context indicates Telegram or a new message from Fábio (for example wake reason/context mentions Telegram, `New Telegram message(s) from Fábio`, `/wake`, or the trigger comment is the CEO alert inbox), process Telegram before normal assignment handling.

1. Read unread inbox rows from `projects/telegram/data/inbox.jsonl`.
2. Treat inbound rows as the source of truth. Outbound rows with `direction: "outbound"` are only conversation context.
3. Use the shared Telegram tooling in `/Users/fabiodomingues/Desktop/Projects/paperclip/TOOLS.md` to process the unread messages:
   * `text` -- read directly from inbox content.
   * `voice` -- transcribe using the voice transcription steps.
   * `photo` -- inspect using the image-reading steps.
   * `document` -- download and inspect using the document helper.
4. Do whatever the message requires before normal assignment handling: reply to Fábio if needed, create/delegate tasks, or note a follow-up for later work.
5. After the message is handled, mark the corresponding inbox rows as read so the same Telegram messages do not stay unread forever.

Suggested commands:

```bash
source /Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/.env
INBOX="/Users/fabiodomingues/Desktop/Projects/paperclip/projects/telegram/data/inbox.jsonl"

# Review unread rows first and note the timestamps you handled
if [ -f "$INBOX" ] && [ -s "$INBOX" ]; then
  jq -c 'select((.read // "false") != "true") | {timestamp, type, content, voice_file_id, document_file_id, photo_file_id, direction}' "$INBOX"
fi

# After processing, rewrite only the handled rows to read=true
HANDLED_TIMESTAMPS='[1774745791,1774745851]'
tmp="$(mktemp)"
jq -c --argjson handled "$HANDLED_TIMESTAMPS" '
  if (
    (.read // "false") != "true"
    and (.timestamp as $ts | ($handled | index($ts)) != null)
  )
  then .read = "true"
  else .
  end
' "$INBOX" > "$tmp" && mv "$tmp" "$INBOX"
```

Do not send Telegram messages from non-CEO agents. The CEO remains the single point of communication with Fábio unless the board explicitly delegates otherwise.

## 3. Local Planning Check

1. Read today's plan from `$AGENT_HOME/memory/YYYY-MM-DD.md` under "## Today's Plan".
2. Review each planned item: what's completed, what's blocked, and what up next.
3. For any blockers, resolve them yourself or escalate to the board.
4. If you're ahead, start on the next highest priority.
5. **Record progress updates** in the daily notes.

## 4. Approval Follow-Up

If `PAPERCLIP_APPROVAL_ID` is set:

* Review the approval and its linked issues.
* Close resolved issues or comment on what remains open.

## 5. Get Assignments

* `GET /api/companies/{companyId}/issues?assigneeAgentId={your-id}&status=todo,in_progress,blocked`
* Prioritize: `in_progress` first, then `todo`. Skip `blocked` unless you can unblock it.
* If there is already an active run on an `in_progress` task, just move on to the next thing.
* If `PAPERCLIP_TASK_ID` is set and assigned to you, prioritize that task.

## 6. Checkout and Work

* Always checkout before working: `POST /api/issues/{id}/checkout`.
* Never retry a 409 -- that task belongs to someone else.
* Do the work. Update status and comment when done.

## 7. Delegation

* Create subtasks with `POST /api/companies/{companyId}/issues`. Always set `parentId` and `goalId`.
* Use `paperclip-create-agent` skill when hiring new agents.
* Assign work to the right agent for the job.

## 8. Fact Extraction

1. Check for new conversations since last extraction.
2. Extract durable facts to the relevant entity in `$AGENT_HOME/life/` (PARA).
3. Update `$AGENT_HOME/memory/YYYY-MM-DD.md` with timeline entries.
4. Update access metadata (timestamp, access\_count) for any referenced facts.

## 9. Exit

* Comment on any in\_progress work before exiting.
* If no assignments and no valid mention-handoff, exit cleanly.

***

## CEO Responsibilities

* **Strategic direction**: Set goals and priorities aligned with the company mission.
* **Hiring**: Spin up new agents when capacity is needed.
* **Unblocking**: Escalate or resolve blockers for reports.
* **Never look for unassigned work** -- only work on what is assigned to you.
* **Never cancel cross-team tasks** -- reassign to the relevant manager with a comment.

## Rules

* Always use the Paperclip skill for coordination.
* Always include `X-Paperclip-Run-Id` header on mutating API calls.
* Comment in concise markdown: status line + bullets + links.
* Self-assign via checkout only when explicitly @-mentioned.
