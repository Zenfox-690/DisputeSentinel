from pydantic import BaseModel
from typing import Dict, Any, Optional


class WebhookEventPayload(BaseModel):
    id: str
    event_version: Optional[str] = None
    create_time: Optional[str] = None
    resource_type: Optional[str] = None
    event_type: str
    summary: Optional[str] = None
    resource: Dict[str, Any]


class WebhookVerificationResponse(BaseModel):
    verification_status: str  # "SUCCESS" or "FAILURE"
    transmission_id: str
    event_type: str
    action_taken: str
