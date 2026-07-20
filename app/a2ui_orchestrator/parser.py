import json
import os
from dotenv import load_dotenv
from .types import ParsedResponse, MessageType
from .utils import extract_stage_id

load_dotenv()

def parse_agent_response(raw_text: str, stage_id: str = None, total_records: int = None, fields_changed: list = None) -> ParsedResponse:
    # Always use the fallback parser to avoid strands dependency
    extracted_stage_id = extract_stage_id(raw_text) or stage_id
    return _fallback_parse(raw_text, extracted_stage_id, total_records, fields_changed)

def _fallback_parse(raw_text: str, stage_id: str, total_records: int = None, fields_changed: list = None) -> ParsedResponse:
    from .utils import extract_record_count, extract_field_names
    import re
    clean_text = re.sub(r'<thinking>.*?</thinking>\s*', '', raw_text, flags=re.DOTALL).strip()
    clean_text = re.sub(r'---INSTRUCTION---.*', '', clean_text, flags=re.DOTALL).strip()
    
    # Try to extract JSON block first
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', clean_text, re.DOTALL)
    json_str = None
    if json_match:
        json_str = json_match.group(1)
    elif clean_text.startswith('{') and clean_text.endswith('}'):
        json_str = clean_text

    if json_str:
        try:
            data = json.loads(json_str)
            try:
                msg_type = MessageType(data.get("type", "TEXT"))
            except ValueError:
                msg_type = MessageType.TEXT
            
            return ParsedResponse(
                type=msg_type,
                text=data.get("text", clean_text),
                stage_id=data.get("stage_id", stage_id),
                total_records=data.get("total_records", total_records),
                fields_changed=data.get("fields_changed", fields_changed or [])
            )
        except Exception:
            pass # fallback to string matching

    text_lower = clean_text.lower()
    
    if "┌────" in clean_text or "here is what i understood:" in text_lower or "shall i proceed?" in text_lower:
        return ParsedResponse(type=MessageType.CONFIRMATION, text=clean_text, stage_id=stage_id)
    elif "preview is ready" in text_lower or "✅" in clean_text or "review the full diff" in text_lower or "staged your changes" in text_lower or "review the changes" in text_lower:
        return ParsedResponse(
            type=MessageType.STAGED, 
            text=clean_text, 
            stage_id=stage_id,
            total_records=total_records if total_records is not None else extract_record_count(clean_text),
            fields_changed=fields_changed if fields_changed is not None else extract_field_names(clean_text)
        )
    elif "data view is ready" in text_lower or "data for you to view" in text_lower or "prepared the data" in text_lower or "🔍" in clean_text:
        return ParsedResponse(
            type=MessageType.DATA_VIEW,
            text=clean_text,
            stage_id=stage_id,
            total_records=total_records if total_records is not None else extract_record_count(clean_text)
        )
    elif "successfully reverted" in text_lower or "undo is complete" in text_lower or "reverted the changes" in text_lower or "have reverted" in text_lower or "reverted the last change" in text_lower:
        return ParsedResponse(type=MessageType.REVERTED, text=clean_text, stage_id=stage_id)
    elif "validation failed" in text_lower or "errors found" in text_lower:
        return ParsedResponse(type=MessageType.VALIDATION_ERROR, text=clean_text, stage_id=stage_id)

    elif "error" in text_lower and ("not supported" in text_lower or "rejected" in text_lower):
        return ParsedResponse(type=MessageType.ERROR, text=clean_text, stage_id=stage_id)
    elif "?" in clean_text and any(kw in text_lower for kw in ["which", "what", "please specify", "could you", "missing"]):
        return ParsedResponse(type=MessageType.CLARIFICATION, text=clean_text, stage_id=stage_id)
        
    return ParsedResponse(type=MessageType.TEXT, text=clean_text, stage_id=stage_id)
