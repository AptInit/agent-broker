try:
    from agent_broker_v1.config_local import BROKER_CFG_V1 as BROKER_CFG_V1
except ModuleNotFoundError as exc:
    if exc.name != "agent_broker_v1.config_local":
        raise
    BROKER_CFG_V1 = {
        "allowlist": [
            "echo",
            "pwd",
            "sleep",
        ],
    }
