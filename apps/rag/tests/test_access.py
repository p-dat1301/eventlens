from eventlens_rag.adapters.fixture_access import FixtureAccessPolicy
from eventlens_rag.domain.access import CallerScope


def test_fixture_policy_accepts_current_scope_and_rejects_revoked_scope() -> None:
    scope = CallerScope(
        schema_version="caller_scope.v1",
        principal_id="fixture-analyst",
        scope_id="fixture-internal",
        policy_id="fixture-policy",
        policy_version="v1",
    )
    assert FixtureAccessPolicy().allows(scope)
    assert not FixtureAccessPolicy(revoked=True).allows(scope)
