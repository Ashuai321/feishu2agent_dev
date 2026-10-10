---
name: xiaot-bitable-management-dev
description: Manage the seven built-in Feishu Bitable tables and explicitly confirmed DEV extensions within the same fixed Base for the 小T DEV agent. Use for querying, creating, updating, deleting, or analyzing records in that Base; do not use for calendars, PRD, other Bases, or unrelated Feishu work.
---

# 小T DEV 多维表格管理

Use this skill only for the fixed DEV Base and its seven built-in tables listed in the Agent file `xiaot-bitable-data-model.md`. A user-requested additional table can be used only after the current user's authorization verifies it with `discover_tables` in that same Base and the shared DEV configuration MCP applies a versioned `bitable.additional_tables` override. Keep all seven built-ins and the fixed Base unchanged. Read the model file when choosing a built-in table, interpreting a status, or handling `ai_text` categories. Never infer table IDs, field names, option values, people, or record IDs.

Use only the DEV MCP tools explicitly enabled for this Agent. For an additional table, first use the read-only `discover_tables` tool with the current user's authorization, then stage the complete mapping through `workspace-agent-relay-general-functions-dev`; do not use the table until that proposal is separately confirmed with a shared confirmation phrase or configured reaction and `list_tables` shows the applied extension. If the MCP connection or a needed tool is absent or fails, say that the Agent cannot perform the operation; do not claim a read or write succeeded. Do not access PRD, calendars, other Bases, or external data sources.

For each Feishu request, preserve the current `request_id` and `conversation_key` in every Bitable tool call. Follow the Agent Instructions for conversation title and progress/result reporting. Read live field definitions and the target record before preparing any write.

All Bitable API calls must use the current Feishu sender's verified user authorization so Feishu enforces that person's access. Never fall back to the bot's tenant token for reading or writing Bitable data. If the sender has not authorized or the token expired, stop and ask them to authorize; the callback must verify that the authorized `open_id` matches the current sender. For current task and subtask searches, exclude `Finished` by default; include it only when the user explicitly asks, using `include_finished=true`.

For every create, update, or delete, prepare one complete proposal that identifies the table, target record, exact fields and values before and after, and the changelog entry. Show it to the same Feishu requester and wait for that requester's explicit confirmation of that exact proposal. On a clear confirmation, call `confirm_mutation` directly without first rebuilding the proposal or rereading the fields; the service rechecks the target and sender. If the proposal ID is missing from context, omit it only when there is exactly one pending proposal for this same requester and conversation. Confirm deletions one record at a time. If the requester changes the target or values, prepare a new proposal. After execution, read back the result and report only verified outcomes; do not retry an uncertain write automatically.

Use the business meanings and current status/category mappings in the Agent file, but read current select options before writing and use the exact available labels. Ask before using an option whose meaning has not been defined.
