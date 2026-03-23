from __future__ import annotations

from typing import Any


def execute_simulated_tool(tool_name: str, args: dict[str, str]) -> dict[str, Any]:
    """Small simulated tool runner for broker pipeline testing."""
    if tool_name == "fetch_url":
        url = args.get("url", "")
        return {
            "status": "ok",
            "tool": tool_name,
            "url": url,
            "content": f"Simulated fetch result from {url}",
        }

    if tool_name == "web_search":
        query = args.get("query", "")
        return {
            "status": "ok",
            "tool": tool_name,
            "query": query,
            "results": [
                {"title": "Result 1", "url": "https://example.com/1"},
                {"title": "Result 2", "url": "https://example.com/2"},
            ],
        }

    return {
        "status": "ok",
        "tool": tool_name,
        "args": args,
        "message": "Simulated tool executed",
    }
