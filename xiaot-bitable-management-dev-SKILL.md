---
name: xiaot-bitable-management-dev
description: Manage the seven explicitly listed Feishu/Lark Bitable tables for the 小T DEV agent. Use for querying, creating, updating, deleting, or analyzing records in that Base; do not use for calendars, PRD, other Bases, or unrelated Feishu work.
---

# 小T DEV 多维表格管理

Use this skill only for the fixed DEV Base and the seven tables listed in the Agent file `xiaot-bitable-data-model.md`. Read that file when choosing a table, interpreting a status, or handling `ai_text` categories. Never infer table IDs, field names, option values, people, or record IDs.

Use only the DEV MCP tools explicitly enabled for this Agent. If the MCP connection or a needed tool is absent or fails, say that the Agent cannot perform the operation; do not claim a read or write succeeded. Do not access PRD, calendars, other Bases, or external data sources.

For each Feishu or Lark request, preserve the current `request_id` and `conversation_key` in every Bitable tool call. Follow the Agent Instructions for conversation title and progress/result reporting. Read live field definitions and the target record before preparing any write.

All Bitable API calls must use the current sender's verified user authorization so the correct platform enforces that person's access. Detect Feishu vs Lark using the existing identity-resolution flow. Feishu users authorize through the Feishu OAuth app and Feishu API host; Lark users authorize through XiaoT's dedicated Lark OAuth app (`XIAOT_LARK_APP_ID` / `XIAOT_LARK_APP_SECRET`) and Lark API host, separate from other agents' Lark app credentials. Tokens are keyed by platform and that platform's verified `open_id`. Never fall back to a bot tenant token or another person's token. If authorization is missing or expired, send the platform-matched authorization action; the callback must verify both platform and `open_id`, save the token, then resume the original request automatically. Do not ask the user to choose a document or switch between Feishu and Lark documents; the Base and table allowlist in this skill is fixed. For current task and subtask searches, exclude `Finished` by default; include it only when the user explicitly asks, using `include_finished=true`.

For every create, update, or delete, prepare one complete proposal that identifies the table, target record, exact fields and values before and after, and the changelog entry. Show it to the same Feishu/Lark requester and wait for that requester's explicit confirmation of that exact proposal. On a clear confirmation, call `confirm_mutation` directly without first rebuilding the proposal or rereading the fields or record; the service rechecks the target record and sender, performs the operation, and verifies the result. If the proposal ID is missing from context, omit it only when there is exactly one pending proposal for this same requester and conversation. Confirm deletions one record at a time. If the requester changes the target or values, prepare a new proposal. Report only verified outcomes; do not retry an uncertain create or delete automatically.

Use the business meanings and current status/category mappings in the Agent file, but read current select options before writing and use the exact available labels. Ask before using an option whose meaning has not been defined.
