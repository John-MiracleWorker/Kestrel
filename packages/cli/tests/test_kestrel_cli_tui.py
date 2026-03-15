import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kestrel as kestrel_cli_entry
from kestrel_cli import cli_tui as cli_tui_impl


def test_cli_parser_supports_tui_command():
    parser = kestrel_cli_entry.build_parser()

    args = parser.parse_args(["tui"])

    assert args.command == "tui"


def test_build_skill_detail_lines_include_dependencies():
    lines = cli_tui_impl.build_skill_detail_lines(
        {
            "pack_id": "demo-pack",
            "name": "Demo Pack",
            "version": "1.2.3",
            "enabled": True,
            "trusted": True,
            "scope": "user",
            "source_type": "marketplace",
            "components": [{"type": "prompt"}, {"type": "native_tool"}],
            "dependencies": [{"pack_id": "core-pack"}],
            "description": "Reusable instructions for demos.",
        }
    )

    assert any("demo-pack" in line for line in lines)
    assert any("Depends on: core-pack" in line for line in lines)
    assert any("prompt x1" in line for line in lines)
    assert any("native_tool x1" in line for line in lines)


def test_summarize_task_events_filters_to_operator_lines():
    lines = cli_tui_impl.summarize_task_events(
        [
            {"type": "thinking", "content": "internal"},
            {"type": "step_started", "content": "Open the file"},
            {"type": "step_complete", "content": "Patched the file"},
            {"type": "task_complete", "content": "All done"},
        ]
    )

    assert lines == [
        "STEP: Open the file",
        "DONE: Patched the file",
        "COMPLETE: All done",
    ]
