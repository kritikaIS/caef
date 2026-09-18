"""Optional MCP binding for the tool surface in `tools.py` (RESEARCH.md §13).

    python -m server.mcp.server

MCP is **not** a required dependency of this project and is not in
`requirements.txt`. If the package is absent this prints how to install it and
exits without error, because nothing in CAEF needs it: the tools work as plain
functions, the pipeline does not go through them, and no safety property depends
on them.

The binding exposes exactly what `tools.TOOLS` exposes — including `deploy`,
which returns a refusal. Deployment is never an unrestricted model tool.
"""

import sys

from server.mcp import tools


def build_server():
    """Construct an MCP server over the tool surface, or None if MCP is absent."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        return None

    server = FastMCP("caef")
    for name, function in tools.TOOLS.items():
        server.add_tool(function, name=name, description=tools.DESCRIPTIONS[name])
    return server


def main() -> int:
    server = build_server()
    if server is None:
        print(
            "The `mcp` package is not installed, so the MCP adapter did not start.\n"
            "Nothing in CAEF requires it — the same tools are callable directly:\n"
            "    from server.mcp import tools; tools.call('get_capability_registry')\n"
            "To run the adapter:  pip install mcp",
            file=sys.stderr,
        )
        return 0
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
