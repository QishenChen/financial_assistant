"""
MCP Server — exposes the financial document intelligence as an MCP tool.

Usage: add this server to your MCP configuration, then call
`query_financial_docs` from any MCP-compatible IDE.

Run:
    npx -y @modelcontextprotocol/server-filesystem /home/this_account/financial_assistant &
    python3 server/mcp_server.py
"""

import json
import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.executor import execute


def query_financial_docs(query: str) -> dict:
    """
    Query the financial document intelligence platform.

    Args:
        query: Natural language question about financial documents.
               Examples:
               - "List all construction projects mentioned"
               - "Compare R&D spending of BYD vs CATL 2022-2024"
               - "Summarize key risks in the bond prospectus"
               - "Is this annual report CSRC-compliant?"

    Returns:
        dict with: task_type, objective, answer, evidence, confidence, token_usage
    """
    result = execute(query)
    return {
        "task_type": result["task_type"],
        "objective": result["objective"],
        "output_shape": result["output_shape"],
        "answer": result["answer"],
        "evidence": result.get("evidence", []),
        "confidence": result.get("confidence", 0.0),
        "token_usage": result.get("token_usage", {}),
    }


# ── MCP stdio interface ──
def _mcp_stdio_loop():
    """Run an MCP-compatible stdio JSON-RPC loop."""
    import select

    while True:
        # Read a line from stdin
        line = sys.stdin.readline()
        if not line:
            break

        try:
            request = json.loads(line.strip())
            method = request.get("method", "")
            req_id = request.get("id", 0)

            if method == "tools/list":
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": [{
                            "name": "query_financial_docs",
                            "description": (
                                "Query financial documents with natural language. "
                                "Auto-detects task type (extract, reasoning, summarize, quality-assurance). "
                                "Accesses financial reports, contracts, insurance policies, regulatory filings, and research."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "Natural language question about financial documents"
                                    }
                                },
                                "required": ["query"]
                            }
                        }]
                    }
                }
            elif method == "tools/call":
                tool_name = request.get("params", {}).get("name", "")
                arguments = request.get("params", {}).get("arguments", {})
                if tool_name == "query_financial_docs":
                    result = query_financial_docs(arguments.get("query", ""))
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}
                            ]
                        }
                    }
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                    }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"}
                }

            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except json.JSONDecodeError:
            error_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"}
            }
            sys.stdout.write(json.dumps(error_resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    # If --test is passed, run a test query directly
    if "--test" in sys.argv:
        test_query = " ".join(sys.argv[2:]) or "中国建筑有哪些施工项目？"
        print(f"Testing query: {test_query}")
        print(json.dumps(query_financial_docs(test_query), ensure_ascii=False, indent=2))
    else:
        _mcp_stdio_loop()