from js_ts_mcp import __version__


def test_version():
    assert __version__ == "0.2.0"


def test_server_imports_and_has_health_tool():
    from js_ts_mcp.server import mcp
    tools = mcp._tool_manager.list_tools()
    names = [t.name for t in tools]
    assert "health_check" in names
