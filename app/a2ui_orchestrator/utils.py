import re
import uuid
from datetime import datetime, timezone

def extract_stage_id(text: str) -> str:
    match = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text, re.IGNORECASE)
    return match.group(0) if match else None

def extract_record_count(text: str) -> int:
    match = re.search(r'Records to update\s*:\s*(\d+)', text, re.IGNORECASE)
    if not match:
        match = re.search(r'(\d+)\s*total records staged', text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_field_names(text: str) -> list:
    # Try to extract from "Change: field → value" patterns in confirmation cards
    change_matches = re.findall(r'Change:\s*(\w+)\s*[→\->]', text)
    if change_matches:
        return list(dict.fromkeys(change_matches))  # deduplicate, preserve order
    
    # Fallback: check against known RM fields
    known_fields = [
        "hardAnchor", "fareAnchor", "B2BBackstop", "B2CBackstop", "plfThreshold",
        "CurveID", "obSeats", "obFare", "BookedLoad", "StrategyReference",
        "AutoTimeRangeFlag", "CugFlag", "TimeWindowRange",
        "CarrExlusionB2C", "CarrExlusionB2B", "flightExclusionB2C", "flightExclusionB2B",
        "forecastedplf", "forecastAllocFlag", "afForecastAllocFlag", "CanAdjustedForecast"
    ]
    found = [f for f in known_fields if f in text]
    return found

def generate_message_id() -> str:
    return str(uuid.uuid4())

def get_iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
