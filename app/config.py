import os

MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8001/v1/agent")
RM_WEBSERVICE_URL = os.getenv("RM_WEBSERVICE_URL", "http://localhost:8082/api/rm-webservice")
