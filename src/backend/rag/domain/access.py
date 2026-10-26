"""Fixture-only caller policy identity boundary."""

# ruff: noqa: EM101

from typing import Literal, LiteralString, Self

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from backend.shared.contracts.base import ContractModel, NonEmptyText


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class CallerScope(ContractModel):
    """Frozen local fixture principal and policy identity, not multi-tenant access."""

    schema_version: Literal["caller_scope.v1"]
    principal_id: Literal["fixture-analyst"]
    scope_id: Literal["fixture-internal"]
    policy_id: NonEmptyText
    policy_version: NonEmptyText

    @model_validator(mode="after")
    def enforce_fixture_policy(self) -> Self:
        """Keep T1 scope boundary explicitly fixture-only."""
        if self.policy_id != "fixture-policy" or self.policy_version != "v1":
            raise _error(
                "unsupported_fixture_policy",
                "caller scope only permits fixture-policy v1",
            )
        return self
