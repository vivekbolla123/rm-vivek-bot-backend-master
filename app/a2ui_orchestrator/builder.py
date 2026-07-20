from .types import ParsedResponse, A2UIMessage, MessageType
from .utils import generate_message_id, get_iso_timestamp

class UIBuilder:
    def __init__(self, theme="default"):
        self.schema = {"theme": theme, "components": [], "actions": []}

    def theme(self, theme: str):
        self.schema["theme"] = theme
        return self

    def text(self, content: str):
        self.schema["components"].append({"type": "text", "content": content})
        return self
        
    def markdown(self, content: str):
        self.schema["components"].append({"type": "markdown", "content": content})
        return self

    def heading(self, content: str):
        self.schema["components"].append({"type": "heading", "content": content})
        return self

    def pre(self, content: str):
        self.schema["components"].append({"type": "pre", "content": content})
        return self

    def add_action(self, label: str, action_type: str, payload: str, style="primary", stage_id=None):
        action = {"label": label, "style": style, "actionType": action_type, "payload": payload}
        if stage_id:
            action["stageId"] = stage_id
        self.schema["actions"].append(action)
        return self

    def build(self):
        return self.schema

def build_a2ui_messages(parsed_response: ParsedResponse, session_id: str = "") -> list[dict]:
    # Returns a list of dicts that can be easily JSON serialized
    messages = []
    
    base_msg = {
        "id": generate_message_id(),
        "type": parsed_response.type.value,
        "content": parsed_response.text,
        "timestamp": get_iso_timestamp(),
        "stage_id": parsed_response.stage_id,
        "metadata": {}
    }
    
    ui = UIBuilder()
    stage_ids = [parsed_response.stage_id] if parsed_response.stage_id else []
    
    if parsed_response.type == MessageType.STAGED:
        base_msg["metadata"] = {
            "total_records": parsed_response.total_records,
            "fields_changed": parsed_response.fields_changed,
            "sample_changes": parsed_response.sample_changes,
            "action": "show_preview",
            "sessionId": session_id,
            "stageIds": stage_ids,
            "statusFilter": "changed",
            "fields": parsed_response.fields_changed or [],
            "message": parsed_response.text
        }
        ui.theme("info").text(parsed_response.text)\
          .add_action("Review Changes", "show_preview", parsed_response.stage_id)\
          .add_action("Reject", "send_text", "cancel", style="secondary", stage_id=parsed_response.stage_id)
        
    elif parsed_response.type == MessageType.DATA_VIEW:
        base_msg["metadata"] = {
            "total_records": parsed_response.total_records,
            "sample_changes": parsed_response.sample_changes,
            "action": "data_fetched",
            "sessionId": session_id,
            "stageIds": stage_ids,
            "statusFilter": "all",
            "fields": [],
            "message": parsed_response.text
        }
        ui.theme("info").text(parsed_response.text)\
          .add_action("View Data", "show_preview", parsed_response.stage_id)
        
    elif parsed_response.type == MessageType.CONFIRMATION:
        ui.markdown(parsed_response.text)\
          .add_action("Proceed", "send_text", "yes")\
          .add_action("Cancel", "send_text", "cancel", style="secondary")
        
    elif parsed_response.type == MessageType.VALIDATION_ERROR:
        base_msg["metadata"] = {"errors": parsed_response.errors}
        ui.theme("error").pre(parsed_response.text)
        
    elif parsed_response.type == MessageType.ERROR:
        ui.theme("error").pre(parsed_response.text)
        
    elif parsed_response.type == MessageType.REVERTED:
        base_msg["metadata"] = {
            "action": "show_summary",
            "sessionId": session_id,
            "stageIds": stage_ids,
            "message": parsed_response.text
        }
        ui.theme("info").text(parsed_response.text)\
          .add_action("View Records", "show_reverted_preview", parsed_response.stage_id)
        
    elif parsed_response.type == MessageType.SUCCESS:
        ui.theme("success").heading("✅ Success").markdown(parsed_response.text)
        if parsed_response.stage_id:
            ui.add_action("View Records", "show_preview", parsed_response.stage_id)
        
    elif parsed_response.type == MessageType.CLARIFICATION:
        ui.theme("clarification").text(parsed_response.text)
        
    else:
        # Default TEXT handling
        ui.text(parsed_response.text)

    base_msg["metadata"]["ui_schema"] = ui.build()
    messages.append(base_msg)
    
    return messages
