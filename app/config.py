import os
import yaml
from pathlib import Path

# Load application-local.yml if it exists
config_data = {}
app_yml_path = Path(__file__).parent.parent / "application-local.yml"
if app_yml_path.exists():
    try:
        with open(app_yml_path, "r") as f:
            config_data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Warning: Failed to load application-local.yml: {e}")

def get_config(key, default):
    # OS Environment takes precedence over application.yml, then default
    return os.getenv(key, config_data.get(key, default))

MOCK_MODE = get_config("MOCK_MODE", "false").lower() == "true"
RM_WEBSERVICE_URL = get_config("RM_WEBSERVICE_URL", "")
RM_BULK_ACTION_AGENT_ARN = get_config("RM_BULK_ACTION_AGENT_ARN", "")
AGENTCORE_REGION = get_config("AGENTCORE_REGION", "")
AGENTCORE_QUALIFIER = get_config("AGENTCORE_QUALIFIER", "")
AGENTCORE_READ_TIMEOUT = int(get_config("AGENTCORE_READ_TIMEOUT", "120"))

REDIS_HOST = get_config("REDIS_HOST", "")
REDIS_PORT = int(get_config("REDIS_PORT", "6379"))
REDIS_PASSWORD = get_config("REDIS_PASSWORD", "")

if not RM_BULK_ACTION_AGENT_ARN and not MOCK_MODE:
    raise RuntimeError(
        "RM_BULK_ACTION_AGENT_ARN is not set. Set it via env var, manifest.yml, or application.yml before starting the app."
    )
