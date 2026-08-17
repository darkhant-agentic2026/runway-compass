"""Connection manager, ticket store, stream broker, and resume replay.

docs/01-architecture.md#layering-inside-coach-api. The one rule worth restating: nothing
in here may cancel generation. `TurnRegistry` owns the task, `StreamBroker` owns the
fan-out, and a socket closing is a subscriber leaving — see `coach.ws.registry`.

**No re-exports here, deliberately.** `coach.ws.manager` needs `TurnService` for its type
hints and `coach.services.turns` needs `StreamBroker` and `TurnRegistry`, so hoisting the
submodules into this file makes importing *either* side a cycle. Importing the module you
want directly costs one longer line and keeps the dependency honest: `services/` uses the
broker and the registry, and only the manager knows about both directions.
"""
