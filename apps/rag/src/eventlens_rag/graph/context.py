"""Validated caller context for one persistence invocation."""

from eventlens_contracts.base import ContractModel
from eventlens_rag.domain.access import CallerScope


class PersistenceContext(ContractModel):
    """Server-owned caller scope, isolated from graph state."""

    caller: CallerScope
