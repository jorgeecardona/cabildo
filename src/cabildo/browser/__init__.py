"""The browser that agents use: status, reap, login, doctor and the MCP server.

The browser is the one shared resource of the machine that is not in cabildo's database.
``entry`` reads its configuration, ``fleet`` finds and stops its processes, ``report`` formats
the output.
"""
