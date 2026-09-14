from eventlens_api.application import HealthStatus, get_health_status


def test_health_use_case_reports_ok_without_web_framework() -> None:
    # Given / When
    status = get_health_status()

    # Then
    assert status == HealthStatus(status="ok")
