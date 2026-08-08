"""AMS gRPC control-plane process (P2).

A separate asyncio process from the FastAPI REST app (design note §1): the two
never share an event loop. Their only coupling point is the database — REST
transition actions write `agent_commands` outbox rows, and this process drains
them onto the live agent session.
"""
