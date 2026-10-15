"""가짜 공급사 호출이 사용자의 실제 Langfuse 프로젝트로 전송되지 않도록 격리한다."""
import pytest


@pytest.fixture(autouse=True)
def offline_observability_environment(monkeypatch):
    # 빈 값은 load_dotenv(override=False)가 실제 로컬 인증 정보를 읽는 것도 막는다.
    # 전송기 테스트는 모의 엔드포인트를 명시적으로 설정한다.
    for name in ('LANGFUSE_BASE_URL', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
        monkeypatch.setenv(name, '')
    for name in ('VIVI_REASONING_EFFORT', 'VIVI_MAX_OUTPUT_TOKENS', 'VIVI_REASONING_EXCLUDE'):
        monkeypatch.setenv(name, '')
