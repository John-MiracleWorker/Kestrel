from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kestrel-stdio-test")


@mcp.tool()
def echo(message: str) -> str:
    return f"echo:{message}"


if __name__ == "__main__":
    mcp.run()
