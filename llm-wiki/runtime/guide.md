# 실행과 비용

실행 설정의 예시는 [.env.example](../../.env.example)이다. 로컬 `.env`의 값이나 실제 호출 인증값을 문서에 복사하지 않는다. API는 키 없이도 조회·액션 관리가 가능하며 새 생성 요청에는 명시적 `fallback`을 반환한다.

```sh
uv sync --frozen
npm ci --prefix frontend
npm run build --prefix frontend
uv run uvicorn vivi_agent.api:app --host 127.0.0.1 --port 8000
```

화면 개발 서버는 `npm run dev --prefix frontend`로 실행한다. [vite.config.ts](../../frontend/vite.config.ts)는 `/api`를 로컬 8000 포트로 전달한다. 빌드한 UI는 FastAPI가 제공한다.

| 파일 | 역할 |
| --- | --- |
| [api.py](../../src/vivi_agent/api.py) | 입력 검증, 목록·상세·제안·액션 API, 정적 UI 제공 |
| [App.tsx](../../frontend/src/App.tsx) | 조회, 생성, 검토, 저장, 상태 변경 화면 |
| [ActionComparison.tsx](../../frontend/src/ActionComparison.tsx) | 같은 문제의 후보와 선정·보류·배제 이유 표시 |
| [ExecutionDetails.tsx](../../frontend/src/ExecutionDetails.tsx) | 실행·자산·측정 계약 표시 |
| [types.ts](../../frontend/src/types.ts) | 서버 결과의 프런트 타입 |
| [styles.css](../../frontend/src/styles.css) | 과제 CI 색상과 화면 배치 |

화면과 API에서 업무 우선순위나 성공 기준을 다시 계산하지 않는다. 서버가 반환한 계약을 표시한다. 새 필드를 추가할 때는 과거 저장 결과에 없는 경우도 처리한다.

## Docker

[Dockerfile](../../Dockerfile)은 Node 단계에서 UI를 빌드한 뒤 Python 이미지에 복사한다. Python 의존성은 `uv.lock`으로 고정한다. 앱 파일시스템은 읽기 전용이고 상태만 볼륨에 쓴다. [compose.yaml](../../compose.yaml)의 `web`은 단일 워커로 실행되며 `127.0.0.1:8000`에만 포트를 연다.

```sh
docker compose up --build -d web
docker compose down
```

`agent-state` 볼륨이 `.vivi` 상태를 보관한다. `down -v`는 이 상태를 지우므로 일반 종료 명령으로 쓰지 않는다. `agent` 서비스는 `cli` 프로필로 별도 실행한다.

## 모델 설정과 두 원장

검증한 운영 프로필은 `OPENROUTER_MODEL=openai/gpt-5.4`, `VIVI_REASONING_EFFORT=low`, `VIVI_MAX_OUTPUT_TOKENS=16000`이다. `.env.example`을 실제 환경에 적용해야 하며, [provider.py](../../src/vivi_agent/provider.py)의 환경값 생략 시 기본값과 혼동하지 않는다. 출력 상한은 예약 계산과 잘림 방지에 쓰며 미사용 토큰을 실제 사용량으로 정산하지 않는다.

`ledger.py`는 과제의 회당 AI-M $0.018, 고객별 월 500회, 요청별 최대 1회 시도를 제한한다. `actual_budget.py`는 실제 달러를 별도로 제한한다. 기본 총원가 상한 $20에서 고정비 $10.50과 추가 Langfuse 배분액을 먼저 뺀 뒤 공급사 최대 예상 요금을 예약한다. 실제 영수증의 비용으로 정산하며 실패한 생성도 청구되면 비용에 포함한다. 과금이 불명확하면 예약을 유지하고 무료 실패로 처리하지 않는다.

SDK 자동 재시도와 공급사 대체 호출은 꺼져 있다. 정확한 캐시나 정책 판단으로 0회 종료할 수 있지만, `fallback`을 좋은 새 추천과 같은 품질로 계산하지 않는다. 토큰 최적화로 실제 요금이 내려가도 과제 S/M/L 환산 단가는 자동으로 바뀌지 않는다.

## 호출 관측

[compose.langfuse.yaml](../../compose.langfuse.yaml)은 선택형 Langfuse와 PostgreSQL·ClickHouse·Redis·MinIO를 띄운다. [compose.observability.yaml](../../compose.observability.yaml)은 앱 컨테이너를 같은 네트워크에 연결한다. 앱 내부 주소와 브라우저 주소를 구분한다.

설정과 실행 명령은 [Langfuse 안내](../../docs/langfuse.md)를 따른다. [observability.py](../../src/vivi_agent/observability.py)가 호출 비용·토큰·지연을 기록한다. 관측 전송에 실패해도 모델을 재호출하지 않는다. 관측 스택의 월 실비는 과제 인프라 가정과 별도로 확인해야 한다.

변경 영향: 모델·추론·출력 상한을 바꾸면 비용 추정, 캐시 식별, 실제 호출 품질 검토를 함께 확인한다. 예산 경계를 바꿀 때는 동시 예약, 미확인 청구, 월 경계 테스트를 포함한다. Docker 변경 시에는 키를 출력하지 않는 구성 검사와 상태 볼륨 보존 여부를 확인한다.
