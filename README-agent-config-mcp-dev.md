# DEV shared Agent rule management

This feature adds a distinct, DEV-only MCP resource for the two DEV Agents:

- XiaoC: `Yuanbo Calendar Manager_DEV` (`agent_scope=xiaoc`)
- XiaoT: `小T多维表格管理_DEV` (`agent_scope=xiaot`)
- MCP URL: `https://bot.boooe.com/agent-config/mcp`
- MCP name: `workspace-agent-relay-general-functions-dev`
- OAuth protected-resource metadata: `https://bot.boooe.com/.well-known/oauth-protected-resource/agent-config/mcp`

The OAuth access token is bound to the `/agent-config/mcp` resource. The existing relay MCP remains at `/mcp`; its authorization tokens and tools do not gain access to the new resource. Both endpoints use the existing DEV OAuth authorization server and DEV D1 binding.

## Authorization

Every call must include `request_id` and `conversation_key` from the current Feishu/Lark run. The MCP resolves the Agent scope, platform, and sender open ID from the persisted event; caller-supplied identity is not accepted.

Reads and changes are limited to the verified current DEV Agent scope. Any verified Feishu/Lark sender can prepare a proposal; only that same sender, in the same conversation, can apply it with a separate exact confirmation before expiry. No administrator allowlist or `AGENT_CONFIG_ADMIN_ALLOWLIST` secret is used.

## Change lifecycle

1. The Agent locates a native rule in its own Instructions/Skills and reports the exact source, current value, proposed value, and impact.
2. `prepare_agent_rule_change` creates a 15-minute, one-key proposal. No runtime value changes yet.
3. The user reviews the proposal and confirms in a separate turn, either with a whole-message shared confirmation phrase such as `确认配置` or a configured confirmation emoji reaction on the exact pending proposal message. A shared refusal phrase or refusal reaction cancels the proposal.
4. `confirm_agent_rule_change` validates the separate confirmation or server-validated reaction in the same conversation, then applies only if the stored version has not changed.
5. An SQLite trigger writes the new version/audit row and marks the proposal applied in the same database statement. The value is injected into future runs for that DEV Agent only.
6. `list_agent_rule_versions` shows history. `prepare_agent_rule_rollback` stages a rollback; it also requires a separate `确认配置`. A rollback appends a new version.

Resetting a runtime override returns behavior to the Agent's existing Instructions/Skills. This MCP does not read or edit ChatGPT-hosted attachments, change per-user memory, grant new tools, or change any PRD configuration.

## Skill package

Attach the same `agent-config-management-dev` Skill to both DEV Agents. Keep each Agent's existing apps and Skills. Connect the new MCP app to both Agents in addition to the existing relay MCP. The package is `skills/agent-config-management-dev.zip`.

## Cloudflare data

The additive D1 migration is `cloudflare_worker/migrations/0010_agent_config_management.sql`. The MCP initializes its tables/triggers lazily when a configuration tool is first called; ordinary Feishu task processing only reads active overrides and does not run schema changes. Apply the migration before deploying the DEV Worker. This is a source change only until the DEV Worker is separately deployed. No PRD files, variables, secrets, agents, or Worker are part of this feature.
