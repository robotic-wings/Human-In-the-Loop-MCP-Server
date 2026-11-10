import json
from pathlib import Path

from central_http_service import CentralServiceConfig, create_lifecycle_app

root = Path(r"C:\Users\MCPTools\hitl-central-physical-e2e")
config = CentralServiceConfig.from_mapping(
    json.loads((root / "private" / "central-config.json").read_text(encoding="utf-8"))
)
try:
    create_lifecycle_app(config)
except Exception as exc:
    factory = f"{type(exc).__name__}: {exc}"
else:
    factory = "unexpectedly-created"
print(json.dumps({
    "config_parse": "ok",
    "route": config.route_mode,
    "endpoint": config.web_base_url,
    "factory": factory,
}))
