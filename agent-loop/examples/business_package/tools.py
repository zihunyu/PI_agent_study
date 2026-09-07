"""A fictional read service. Replace the injected client in a real application."""

from pi_agent_loop import AgentTool, AgentToolResult


def tool_factories(client):
    def lookup():
        def validate(arguments):
            if not isinstance(arguments, dict) or set(arguments) != {"key"}:
                raise ValueError("Exactly one key is required")
            if not isinstance(arguments["key"], str) or not arguments["key"].strip():
                raise ValueError("key must be non-empty text")
            return dict(arguments)

        async def execute(_call_id, arguments, cancellation, _update):
            cancellation.throw_if_cancelled()
            value = await client.lookup(arguments["key"])
            return AgentToolResult(
                content=[{"type": "text", "text": value}], details={"value": value}
            )

        return AgentTool(
            name="fictional_lookup",
            label="Fictional lookup",
            description="Read a fictional catalogue entry",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            validate_args=validate,
            execute=execute,
            replay_policy="safe",
        )

    return {"fictional_lookup": lookup}
