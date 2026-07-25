from __future__ import annotations

from contextvars import ContextVar

from .models import Principal

current_principal: ContextVar[Principal] = ContextVar("jenkinsservice_principal")
