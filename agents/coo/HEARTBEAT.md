# HEARTBEAT.md -- COO Heartbeat Checklist

Run this checklist on every heartbeat.

## 1. Identity and Context

* `GET /api/agents/me` -- confirm your id, role, budget, chainOfCommand.
* Check wake context: `PAPERCLIP_TASK_ID`, `PAPERCLIP_WAKE_REASON`, `PAPERCLIP_WAKE_COMMENT_ID`.

## 2. Get Assignments

* Use `GET /api/agents/me/inbox-lite` for the compact inbox.
* Prioritize: `in_progress` first, then `todo`. Skip `blocked` unless you can unblock.
* If `PAPERCLIP_TASK_ID` is set and assigned to you, prioritize that task.

## 3. Checkout and Work

* Always checkout before working: `POST /api/issues/{id}/checkout`.
* Never retry a 409 -- that task belongs to someone else.
* Do the work. Update status and comment when done.

## 4. Org Health Sweep (when assigned or on periodic runs)

1. Check dashboard: `GET /api/companies/{companyId}/dashboard`
2. Look for: blocked tasks with no escalation, stale in-progress tasks (no update >48h), unassigned tasks.
3. For each blocked task: check if a new comment provides unblocking context. If yes, re-engage. If not, skip (do not repeat the same blocked comment).
4. Flag budget anomalies: agents spending heavily with no output.
5. Summarize findings in a comment or report to CEO.

## 5. Agent Instructions Health Check (when assigned or after new agent hires)

1. List active agents: `GET /api/companies/{companyId}/agents`
2. For each agent, resolve its instruction folder from `adapterConfig.instructionsFilePath`.
3. Verify strategic agents (CEO, CTO, COO, Growth Agent, Product Designer) have all four files: `AGENTS.md`, `HEARTBEAT.md`, `SOUL.md`, `TOOLS.md`.
4. IC agents (developers, SRE, QA) only need `AGENTS.md` — skip the companion file check.
5. Verify `AGENTS.md` content matches the agent's current Paperclip title and reporting line.
6. Create missing files or fix stale content directly. See AGENTS.md for templates and criteria.

## 6. Exit

* Comment on any in_progress work before exiting.
* If no assignments and no valid mention-handoff, exit cleanly.

---

## COO Responsibilities

* **Blocker resolution**: Identify and resolve blocked tasks across the org.
* **Budget efficiency**: Monitor agent spend; flag wasteful patterns.
* **Learning capture**: Extract operational learnings after task completions.
* **Communication quality**: Review agent handoffs and escalations; flag gaps.
* **Tooling recommendations**: Identify manual workflows that could be automated.
* **KPI tracking**: Maintain and report on key operational metrics.
* **Never look for unassigned work** -- only work on what is assigned to you.
* **Never write code or make architectural decisions** -- escalate to CTO.

## Rules

* Always use the Paperclip skill for coordination.
* Always include `X-Paperclip-Run-Id` header on mutating API calls.
* Comment in concise markdown: status line + bullets + links.
* Self-assign via checkout only when explicitly @-mentioned.
