try:
    from agent_broker_v2.config_local import BROKER_CFG_V2 as BROKER_CFG_V2
except ModuleNotFoundError as exc:
    if exc.name != "agent_broker_v2.config_local":
        raise
    BROKER_CFG_V2 = {
        "allowlist": [
            "echo",
            "pwd",
            "sleep",
        ],
    }
