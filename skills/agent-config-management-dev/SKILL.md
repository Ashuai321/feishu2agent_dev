---
name: agent-config-management-dev
description: Inspect and safely manage shared runtime rules for the DEV XiaoC and XiaoT Feishu Agents. Use when a user asks what an Agent rule says, where it comes from, to change a shared Agent rule, review its versions, or roll it back.
---

# DEV Agent Configuration Management

This one Skill is shared by **Yuanbo Calendar Manager_DEV (小C)** and **小T多维表格管理_DEV**. It is for DEV only. Never use this workflow for PRD or claim it edits PRD.

This is a narrow exception to each Agent's normal tool scope: use the configuration MCP only when the user explicitly asks to inspect or change that Agent's shared rules. For ordinary calendar requests, keep 小C's existing calendar workflow; for ordinary data requests, keep 小T's existing Bitable workflow. Do not route ordinary work through this MCP.

The dedicated MCP is `workspace-agent-relay-general-functions-dev` at `https://bot.boooe.com/agent-config/mcp`. It is separate from the existing relay MCP at `/mcp`. Use the current Feishu/Lark run's exact `request_id` and `conversation_key` for every call. The service determines whether the run belongs to `xiaoc` or `xiaot`; never provide or guess an Agent scope or a user's identity.

## What this service changes

The MCP keeps explicitly user-confirmed, per-Agent runtime overrides in the DEV database. Any verified Feishu/Lark sender can prepare a change; the same sender must separately confirm it in the same conversation. An active override is supplied to future Feishu/Lark turns for that same DEV Agent. 小C and 小T have separate scopes, so changing a key for one never changes the other.

This service **does not read or edit ChatGPT-hosted Instructions or Skill attachments**. To identify a native rule, inspect the current Agent's available Instructions and Skills yourself, then report the exact document/section and native value. Use `inspect_agent_rule` to check whether that rule also has a managed runtime override. When a rule has no override, the existing Instructions/Skills remain the source of behavior.

Keep these categories distinct:

- A personal preference or alias for the current user continues to use the existing per-user memory workflow. Do not turn it into a shared Agent rule.
- A shared rule that should apply to future users of this DEV Agent uses this configuration MCP.
- An actual edit to the Agent's hosted Instructions or Skill files is outside this MCP. Do not claim such an attachment was edited.
- The configuration MCP cannot change fixed safety, authorization, environment, credential, or tool-access restrictions.

An active shared value applies only to its matching rule. Explicit instructions from the current user and that sender's verified personal preferences take precedence over a shared default. If an active rule would contradict a higher-priority restriction, do not apply it; explain the conflict.

For the existing calendar timezone rule, use the stable key `default_timezone` so its saved versions and rollback history stay together. Use this precedence: explicit timezone in the current request, then the current sender's verified personal timezone preference, then this Agent's active managed override for `default_timezone`, then the existing Instructions/Skills and connected-calendar fallback. Do not create a parallel `calendar.default_timezone` key. If the native Instructions/Skills do not define one shared default, say so; a user-approved override would add a new runtime default and would not create or edit a hosted source rule.

## Inspect a rule

1. Read the current Agent's own Instructions and attached Skills; do not inspect the other Agent's materials as a substitute.
2. Identify the rule's native value and exact source location. If the request does not identify a rule clearly, ask which rule they mean.
3. Call `inspect_agent_rule` with the stable key (for example, `default_timezone`) and current run context. Explain both the native source value and the active managed override, if any. Do not describe an override as an edit to the source attachment.
4. Use `list_agent_rules` when the user asks which shared runtime overrides are active. Missing keys mean the original Instructions/Skills continue to apply.

## Propose a change

1. Confirm the request concerns a shared rule for the current DEV Agent, not a personal memory, PRD Agent, tool permission, secret, or hosted attachment edit.
2. Locate and report the native source, current value, proposed value, and impact on future turns. If the source or impact is unclear, ask before preparing a proposal.
   If there is no native shared rule, state that explicitly and label the proposal as adding a new runtime rule; never invent a source location.
3. Call `prepare_agent_rule_change` with a stable lowercase `rule_key`, exact rule name, exact source reference, native current value, proposed value, impact, and `action=set`. To remove an existing runtime override and return to the original Instructions/Skills, use `action=reset` and omit `new_value`.
4. Show the returned proposal ID, Agent scope, source, current native value, existing override (if present), proposed value, impact, and expiry to the user. State that nothing has changed yet.
5. Present the exact proposal and wait for a separate confirmation. Preserve both existing ways to confirm: a whole-message reply matching the current shared confirmation phrases (including `确认配置`), or a configured confirmation emoji reaction on that exact pending proposal message. A configured refusal phrase or refusal emoji reaction cancels it. Never treat a confirmation in the original proposal request, a reaction to another message, or an unconfigured emoji as approval. Do not call the confirmation tool in the proposal turn.
6. On the separate confirmation reply or validated reaction turn, call `confirm_agent_rule_change` with that **new turn's** `request_id` and `conversation_key` and the exact pending `proposal_id`. The service verifies the proposal message, configured shared phrase/reaction, conversation, expiry, and unchanged version. Never substitute a previous run's identifiers.
7. Report the new version only when the tool confirms it was applied. A failed/blocked tool call means no success claim; ordinary Agent tasks remain available.

There is no administrator allowlist. Any verified Feishu/Lark sender can prepare a proposal and confirm their own proposal in a separate message. The service still rejects unverified runs, proposals from another sender or conversation, expired proposals, stale versions, and non-exact confirmations.

## Review and rollback

- Call `list_agent_rule_versions` to show the version history of a specific key.
- To roll back, call `prepare_agent_rule_rollback` for an earlier version (version `0` means clear the override). Show the target value and impact, then require a separate exact `确认配置` reply and call `confirm_agent_rule_change` with that proposal ID.
- A rollback creates a new version; it does not erase history.
- Use `cancel_agent_rule_proposal` if the user declines or changes the request. Do not apply the old proposal after its expiry, a changed target, or a version conflict; inspect the rule and prepare a new proposal instead.

## Safety and continuity

- This MCP is only for shared DEV runtime rules, not Bitable records, calendar events, OAuth tokens, Agent app connections, or user memories.
- Never put secrets, access tokens, app secrets, or credentials in a rule value.
- One proposal changes one key for one Agent. Do not bundle unrelated changes.
- The server uses optimistic version checks and records the actor, source, proposal, operation, and resulting version. Do not retry a confirmation after an ambiguous response; check the version history first.
- If the configuration MCP is unavailable, do not claim to have inspected or changed a shared rule. Continue unrelated existing tasks using their existing workflows.
