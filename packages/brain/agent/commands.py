"""
Chat Commands — /slash commands parsed from channel messages.

Inspired by OpenClaw's chat command system:
  /status    — session status (model, tokens, cost)
  /new       — reset the session
  /compact   — compact session context (summarize old messages)
  /think     — set thinking level (off|low|medium|high)
  /usage     — toggle usage footer (off|tokens|full)
  /model     — switch model
  /help      — list available commands

Commands are parsed before the message reaches the agent,
enabling quick session control without burning tokens.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger("brain.agent.commands")


@dataclass
class CommandResult:
    """Result of a parsed chat command."""
    command: str
    args: list[str]
    response: str
    handled: bool = True  # If True, don't pass to agent
    side_effects: dict[str, Any] = None  # Actions to take (session patches, etc.)

    def __post_init__(self):
        if self.side_effects is None:
            self.side_effects = {}


class CommandParser:
    """
    Parses and executes /slash commands from chat messages.

    Integrates with the gateway to intercept commands before
    they reach the agent, providing zero-latency responses.
    """

    def __init__(self):
        self._commands: dict[str, Callable] = {
            "status": self._cmd_status,
            "new": self._cmd_new,
            "reset": self._cmd_new,  # Alias
            "compact": self._cmd_compact,
            "think": self._cmd_think,
            "usage": self._cmd_usage,
            "model": self._cmd_model,
            "help": self._cmd_help,
            "verbose": self._cmd_verbose,
            "cancel": self._cmd_cancel,
        }

    def is_command(self, message: str) -> bool:
        """Check if a message starts with a / command."""
        return bool(message.strip()) and message.strip().startswith("/")

    def parse(self, message: str, context: dict) -> Optional[CommandResult]:
        """
        Parse a /command from a message.

        Args:
            message: The raw message text
            context: Session context (model, tokens, cost, etc.)

        Returns:
            CommandResult if it's a valid command, None if not a command
        """
        text = message.strip()
        if not text.startswith("/"):
            return None

        # Parse command and args
        parts = text[1:].split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1].split() if len(parts) > 1 else []

        handler = self._commands.get(cmd_name)
        if not handler:
            return CommandResult(
                command=cmd_name,
                args=args,
                response=f"Unknown command: /{cmd_name}. Type /help for available commands.",
                handled=True,
            )

        return handler(args, context)

    # ── Command Handlers ─────────────────────────────────────────────

    def _cmd_status(self, args: list[str], ctx: dict) -> CommandResult:
        """Show session status."""
        model = ctx.get("model", "unknown")
        tokens = ctx.get("total_tokens", 0)
        cost = ctx.get("cost_usd", 0)
        task_status = ctx.get("task_status", "idle")
        session_type = ctx.get("session_type", "main")

        lines = [
            "📊 **Session Status**",
            f"• Model: `{model}`",
            f"• Tokens: {tokens:,}",
            f"• Cost: ${cost:.4f}",
            f"• Status: {task_status}",
            f"• Type: {session_type}",
        ]

        if ctx.get("thinking_level"):
            lines.append(f"• Thinking: {ctx['thinking_level']}")
        if ctx.get("usage_mode"):
            lines.append(f"• Usage display: {ctx['usage_mode']}")

        return CommandResult(
            command="status",
            args=args,
            response="\n".join(lines),
        )

    def _cmd_new(self, args: list[str], ctx: dict) -> CommandResult:
        """Reset the session."""
        return CommandResult(
            command="new",
            args=args,
            response="🔄 Session reset. Starting fresh.",
            side_effects={
                "action": "reset_session",
                "clear_messages": True,
                "clear_context": True,
            },
        )

    def _cmd_compact(self, args: list[str], ctx: dict) -> CommandResult:
        """Compact the session context."""
        return CommandResult(
            command="compact",
            args=args,
            response="📦 Compacting session context...",
            side_effects={
                "action": "compact_context",
            },
        )

    def _cmd_think(self, args: list[str], ctx: dict) -> CommandResult:
        """Set thinking level."""
        levels = ["off", "low", "medium", "high"]

        if not args:
            current = ctx.get("thinking_level", "medium")
            return CommandResult(
                command="think",
                args=args,
                response=f"🧠 Current thinking level: **{current}**\nUsage: `/think off|low|medium|high`",
            )

        level = args[0].lower()
        if level not in levels:
            return CommandResult(
                command="think",
                args=args,
                response=f"Invalid level: {level}. Options: {', '.join(levels)}",
            )

        return CommandResult(
            command="think",
            args=args,
            response=f"🧠 Thinking level set to **{level}**",
            side_effects={
                "action": "set_thinking_level",
                "value": level,
            },
        )

    def _cmd_usage(self, args: list[str], ctx: dict) -> CommandResult:
        """Toggle usage display mode."""
        modes = ["off", "tokens", "full"]

        if not args:
            current = ctx.get("usage_mode", "off")
            return CommandResult(
                command="usage",
                args=args,
                response=f"📈 Current usage mode: **{current}**\nUsage: `/usage off|tokens|full`",
            )

        mode = args[0].lower()
        if mode not in modes:
            return CommandResult(
                command="usage",
                args=args,
                response=f"Invalid mode: {mode}. Options: {', '.join(modes)}",
            )

        return CommandResult(
            command="usage",
            args=args,
            response=f"📈 Usage display set to **{mode}**",
            side_effects={
                "action": "set_usage_mode",
                "value": mode,
            },
        )

    def _cmd_model(self, args: list[str], ctx: dict) -> CommandResult:
        """Switch the active model."""
        if not args:
            current = ctx.get("model", "unknown")
            return CommandResult(
                command="model",
                args=args,
                response=f"🤖 Current model: `{current}`\nUsage: `/model <model_name>`",
            )

        new_model = args[0]
        return CommandResult(
            command="model",
            args=args,
            response=f"🤖 Switching to model: `{new_model}`",
            side_effects={
                "action": "set_model",
                "value": new_model,
            },
        )

    def _cmd_verbose(self, args: list[str], ctx: dict) -> CommandResult:
        """Toggle verbose mode."""
        if not args:
            current = ctx.get("verbose", False)
            return CommandResult(
                command="verbose",
                args=args,
                response=f"🔊 Verbose mode: **{'on' if current else 'off'}**",
            )

        value = args[0].lower() in ("on", "true", "1", "yes")
        return CommandResult(
            command="verbose",
            args=args,
            response=f"🔊 Verbose mode **{'on' if value else 'off'}**",
            side_effects={
                "action": "set_verbose",
                "value": value,
            },
        )

    def _cmd_cancel(self, args: list[str], ctx: dict) -> CommandResult:
        """Cancel the current task."""
        return CommandResult(
            command="cancel",
            args=args,
            response="⏹️ Cancelling current task...",
            side_effects={
                "action": "cancel_task",
            },
        )

    def _cmd_help(self, args: list[str], ctx: dict) -> CommandResult:
        """Show available commands."""
        help_text = """**Available Commands**

| Command | Description |
|---------|-------------|
| `/status` | Show session status (model, tokens, cost) |
| `/new` | Reset the session |
| `/compact` | Compact context to save tokens |
| `/think <level>` | Set thinking level (off/low/medium/high) |
| `/usage <mode>` | Toggle usage footer (off/tokens/full) |
| `/model <name>` | Switch the active model |
| `/verbose on\|off` | Toggle verbose output |
| `/cancel` | Cancel the current task |
| `/help` | Show this help message |"""

        return CommandResult(
            command="help",
            args=args,
            response=help_text,
        )
