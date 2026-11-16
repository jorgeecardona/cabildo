"""A small coding agent for local models behind an OpenAI-compatible endpoint.

It is a client of the bus like Claude Code and Codex: it registers a session, takes a
``file:<path>`` claim before every edit, and releases the claims when it stops.
"""
