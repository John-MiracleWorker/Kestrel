from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class CommandResult:
    command: str
    args: list[str]
    response: str
    handled: bool = True
    side_effects: dict[str, Any] = field(default_factory=dict)


class CommandParser:
    """Standalone CLI slash-command parser.

    The brain service has its own command parser, but the installed CLI should
    not depend on `packages/brain` being importable.
    """

    def __init__(self) -> None:
        self._commands: dict[str, Callable[[list[str], dict[str, Any]], CommandResult]] = {
            "status": self._cmd_status,
            "new": self._cmd_new,
            "reset": self._cmd_new,
            "compact": self._cmd_compact,
            "think": self._cmd_think,
            "usage": self._cmd_usage,
            "model": self._cmd_model,
            "help": self._cmd_help,
            "verbose": self._cmd_verbose,
            "cancel": self._cmd_cancel,
            "exit": self._cmd_exit,
            "quit": self._cmd_exit,
        }

    def is_command(self, message: str) -> bool:
        return bool(message.strip()) and message.strip().startswith("/")

    def parse(self, message: str, context: dict[str, Any]) -> CommandResult | None:
        text = message.strip()
        if not text.startswith("/"):
            return None

        parts = text[1:].split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1].split() if len(parts) > 1 else []
        handler = self._commands.get(cmd_name)
        if handler is None:
            return CommandResult(
                command=cmd_name,
                args=args,
                response=f"Unknown command: /{cmd_name}. Type /help for available commands.",
            )
        return handler(args, context)

    def _cmd_status(self, args: list[str], ctx: dict[str, Any]) -> CommandResult:
        lines = [
            "Session Status",
            f"- Model: `{ctx.get('model', 'unknown')}`",
            f"- Tokens: {ctx.get('total_tokens', 0):,}",
            f"- Cost: ${ctx.get('cost_usd', 0):.4f}",
            f"- Status: {ctx.get('task_status', 'idle')}",
            f"- Type: {ctx.get('session_type', 'main')}",
        ]
        if ctx.get("thinking_level"):
            lines.append(f"- Thinking: {ctx['thinking_level']}")
        if ctx.get("usage_mode"):
            lines.append(f"- Usage display: {ctx['usage_mode']}")
        return CommandResult(command="status", args=args, response="\n".join(lines))

    def _cmd_new(self, args: list[str], _ctx: dict[str, Any]) -> CommandResult:
        return CommandResult(
            command="new",
            args=args,
            response="Session reset. Starting fresh.",
            side_effects={"action": "reset_session", "clear_messages": True, "clear_context": True},
        )

    def _cmd_compact(self, args: list[str], _ctx: dict[str, Any]) -> CommandResult:
        return CommandResult(
            command="compact",
            args=args,
            response="Compacting session context...",
            side_effects={"action": "compact_context"},
        )

    def _cmd_think(self, args: list[str], ctx: dict[str, Any]) -> CommandResult:
        levels = ["off", "low", "medium", "high"]
        if not args:
            current = ctx.get("thinking_level", "medium")
            return CommandResult(
                command="think",
                args=args,
                response=f"Current thinking level: {current}\nUsage: /think off|low|medium|high",
            )
        level = args[0].lower()
        if level not in levels:
            return CommandResult(command="think", args=args, response=f"Invalid level: {level}. Options: {', '.join(levels)}")
        return CommandResult(
            command="think",
            args=args,
            response=f"Thinking level set to {level}",
            side_effects={"action": "set_thinking_level", "value": level},
        )

    def _cmd_usage(self, args: list[str], ctx: dict[str, Any]) -> CommandResult:
        modes = ["off", "tokens", "full"]
        if not args:
            current = ctx.get("usage_mode", "off")
            return CommandResult(
                command="usage",
                args=args,
                response=f"Current usage mode: {current}\nUsage: /usage off|tokens|full",
            )
        mode = args[0].lower()
        if mode not in modes:
            return CommandResult(command="usage", args=args, response=f"Invalid mode: {mode}. Options: {', '.join(modes)}")
        return CommandResult(
            command="usage",
            args=args,
            response=f"Usage display set to {mode}",
            side_effects={"action": "set_usage_mode", "value": mode},
        )

    def _cmd_model(self, args: list[str], ctx: dict[str, Any]) -> CommandResult:
        if not args:
            return CommandResult(
                command="model",
                args=args,
                response=f"Current model: `{ctx.get('model', 'unknown')}`\nUsage: /model <model_name>",
            )
        model = args[0]
        return CommandResult(
            command="model",
            args=args,
            response=f"Switching to model: `{model}`",
            side_effects={"action": "set_model", "value": model},
        )

    def _cmd_verbose(self, args: list[str], ctx: dict[str, Any]) -> CommandResult:
        if not args:
            current = bool(ctx.get("verbose", False))
            return CommandResult(command="verbose", args=args, response=f"Verbose mode: {'on' if current else 'off'}")
        value = args[0].lower() in ("on", "true", "1", "yes")
        return CommandResult(
            command="verbose",
            args=args,
            response=f"Verbose mode {'on' if value else 'off'}",
            side_effects={"action": "set_verbose", "value": value},
        )

    def _cmd_cancel(self, args: list[str], _ctx: dict[str, Any]) -> CommandResult:
        return CommandResult(
            command="cancel",
            args=args,
            response="Cancelling current task...",
            side_effects={"action": "cancel_task"},
        )

    def _cmd_help(self, args: list[str], _ctx: dict[str, Any]) -> CommandResult:
        commands = [
            "/status  Show session state",
            "/new     Reset the session",
            "/compact Compact context",
            "/think   Set thinking level",
            "/usage   Set usage display",
            "/model   Set model label",
            "/verbose Toggle verbose mode",
            "/cancel  Cancel current task",
            "/exit    Close the CLI",
            "/help    Show this help",
        ]
        return CommandResult(command="help", args=args, response="Available commands:\n" + "\n".join(commands))

    def _cmd_exit(self, args: list[str], _ctx: dict[str, Any]) -> CommandResult:
        return CommandResult(
            command="exit",
            args=args,
            response="Closing Kestrel CLI.",
            side_effects={"action": "exit_repl"},
        )
