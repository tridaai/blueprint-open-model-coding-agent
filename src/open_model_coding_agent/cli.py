"""Command line: run the demo, list the gold suite, or serve the
MCP surface over stdio."""

from __future__ import annotations

import sys

from .config import load_settings
from .gold import GoldSuite


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "demo"
    if command == "demo":
        from .demo import main as demo_main

        demo_main()
        return 0
    if command == "mcp":
        from .mcp_server import main as mcp_main

        return mcp_main()
    if command == "gold":
        settings = load_settings()
        suite = GoldSuite(settings.gold_dir)
        problems = suite.check_construction()
        for task in suite.tasks:
            print(f"{task.id}: {task.title} (scope: {', '.join(task.file_scope)})")
        if problems:
            print("construction problems:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(f"gold suite is sound: {len(suite.tasks)} tasks")
        return 0
    print(f"unknown command {command!r}; use 'demo', 'gold', or 'mcp'")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
