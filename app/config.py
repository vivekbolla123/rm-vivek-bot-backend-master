import os

MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8001/v1/agent")
RM_WEBSERVICE_URL = os.getenv("RM_WEBSERVICE_URL", "http://localhost:8082/api/rm-webservice")
RM_BULK_ACTION_AGENT_ARN = os.getenv("RM_BULK_ACTION_AGENT_ARN", "arn:aws:bedrock-agentcore:ap-south-1:891377165721:runtime/rm_bulk_action-lO7EcuHBzC")
AGENTCORE_REGION = os.getenv("AGENTCORE_REGION", "ap-south-1")
AGENTCORE_QUALIFIER = os.getenv("AGENTCORE_QUALIFIER", "DEFAULT")
AGENTCORE_READ_TIMEOUT = int(os.getenv("AGENTCORE_READ_TIMEOUT", "120"))

if not RM_BULK_ACTION_AGENT_ARN and not MOCK_MODE:
    raise RuntimeError(
        "RM_BULK_ACTION_AGENT_ARN is not set. Set it via env var/secret before starting the app."
    )
