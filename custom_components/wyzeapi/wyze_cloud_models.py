from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional, Any


@dataclass
class WyzeCredential:
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    user_id: Optional[str] = None
    mfa_options: Optional[list] = None
    mfa_details: Optional[dict[str, Any]] = None
    sms_session_id: Optional[str] = None
    email_session_id: Optional[str] = None
    phone_id: str = str(uuid.uuid4())

