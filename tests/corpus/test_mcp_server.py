"""Import-level guard: the MCP server module must import and register its tools.

This catches SDK API breakage (e.g. an incompatible mcp version) that a
mock-based API test would miss, since nothing else imports this module.
"""


def test_module_imports_and_registers_tools():
    from corpus import mcp_server

    assert mcp_server.mcp is not None
    # The tool functions are defined at import time.
    for name in ("corpus_search", "corpus_query", "corpus_get", "corpus_stats"):
        assert hasattr(mcp_server, name)


def test_run_is_callable():
    from corpus import mcp_server

    assert callable(mcp_server.run)
