#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Definitions of Task dataclass
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional


@dataclass
class Task:
    id: int
    call: str
    args: Dict[str, Any]
    priority: int
    status: str
    inserted_at: datetime
    started_at: Optional[datetime]
    last_heartbeat: Optional[datetime]
    expected_duration: Optional[timedelta]
