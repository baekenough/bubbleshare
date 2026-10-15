# VIVI Action Workspace

AI 검색 응답에서 개선할 문제를 찾고, 같은 문제를 푸는 액션 후보 3개를 비교해 다음 작업 하나를 추천한다. 추천에는 근거, 선정·배제 이유, 담당 역할, 작업 초안, 완료 조건과 측정 범위가 포함된다.

업무 로직은 LangGraph 에이전트에 있다. FastAPI와 React는 조회·검토·저장·상태 변경을 담당한다. 기본 요청은 모델 최대 1회이며 자동 재시도와 재작성 호출은 없다.

## 실행

`.env.example`을 참고해 `.env`에 키를 설정한다. 검증한 설정은 OpenRouter `openai/gpt-5.4`, `VIVI_REASONING_EFFORT=low`다.

```sh
docker compose up --build -d web
# http://localhost:8000
docker compose down
# 저장된 액션을 보존하려면 -v 옵션을 사용하지 않는다.
```

키가 없어도 조회·저장·상태 변경은 가능하다. 새 초안 생성은 키가 없다는 이유와 함께 fallback을 반환한다. `.env`와 로컬 상태는 Git·Docker 이미지에서 제외한다. OpenAI 직접 호출은 `VIVI_PROVIDER=openai`, `OPENAI_API_KEY`, `VIVI_MODEL`로 설정한다.

로컬 개발:

```sh
uv sync --frozen
npm ci --prefix frontend
npm run build --prefix frontend
uv run uvicorn vivi_agent.api:app --host 127.0.0.1 --port 8000
```

주제 선택 → 근거 확인 → 초안 생성 → 후보·작업·측정 조건 검토 → 저장 → 진행 상태 변경 순서로 사용한다.

## 에이전트 흐름

```text
DuckDB 집계·LatticeDB 관계 조회 → 현재 액션·문제 우선순위 확인
  → 근거 부족 / 새 작업 없음 / 정확한 캐시
  → 과제 단가·실제 USD 예산 예약 → 모델 1회
  → 구조·근거·후보 일관성 검사 → 실행·측정 계약 구성
  → 사용자 검토·저장
```

질문 우선순위는 `질문 중요도 × 추천 공백 × 유효 수집률`이다. 모델은 선정 질문에 대한 서로 다른 개입 방식 3개를 비교한다. 우선순위 점수와 예상 효과는 구분하며, 효과를 매출 예측이나 검증된 성과로 표시하지 않는다.

| 저장소 | 용도 |
| --- | --- |
| DuckDB | 정제된 응답의 지표 집계 |
| LatticeDB | 질문·응답·출처·브랜드 관계 조회 |
| SQLite | 사용자 액션·상태, 과제 단가·실제 비용 원장 |
| JSON / JSONL | 정확한 결과 캐시, 실행·변경 이력 |

주요 성과는 같은 질문·채널 범위의 자사 언급·추천이고, 출처 인용은 보조 지표다. 게시 액션은 확정 URL·자사 source ID·게시 시각이 모두 있어야 후속 수집을 시작한다. 내부 검증 작업에는 게시 후 성과 측정을 붙이지 않는다.

API는 `GET /api/topics`, `GET /api/topics/{topic_id}`, `POST /api/proposals`, `GET/POST /api/actions`, `PATCH /api/actions/{id}`로 구성된다. 고객은 서버의 `VIVI_CUSTOMER_ID`로 고정한다. 인증·조직 관리가 없는 로컬 단일 고객 MVP다.

## 비용과 검증

입력 중복 제거와 생성 계약 축소, 낮은 추론 설정을 적용했다. 같은 네 사례에서 평균 실제 비용은 **$0.1858 → $0.1024, 44.9% 감소**했고 내용 평가도 기존 수준 이상을 유지했다. 더 저렴한 mini·무추론 설정은 제품 사실과 실행 절차 오류가 남아 제외했다.

기본 최대 1회를 유지하며 정확한 캐시·근거 부족·기존 과업 중복은 0회 처리한다. 캐시에는 고객·데이터·맥락·현재 액션·모델 설정·스키마·구현 버전이 포함된다. 과제의 고정 단가 M $0.018과 공급사의 실제 USD 비용은 별도 원장으로 제한한다.

**월 $100 구독·1,000건 추천·마진 80% 조건은 아직 충족하지 못했다.** 현재 실측 평균을 적용하면 평균월 98건 이하만 새 생성하고 나머지 902건은 유효한 캐시나 정당한 0회 판단이어야 한다. 이 재사용률은 검증되지 않았다. 예산 초과 fallback을 같은 품질의 추천으로 세지 않는다.

비용 비교 20회는 총 $1.14256725였고 Langfuse 기록과 20/20 일치했다. [비용·품질 결과](docs/cost-optimization-results.md)에 실패 설정, 과제 계산과 실제 요금, 남은 편집 항목을 함께 기록했다.

```sh
uv run pytest -q
npm run build --prefix frontend
```

테스트 473개와 하위 테스트 18개가 통과했다. 제공된 합성 데이터에 실제 모델을 호출한 검증이다. 실제 제품 연동·게시·노출 개선은 수행하지 않았다. 모든 추천은 사람이 제품 사실과 실행 가능성을 확인할 초안이다.

## Langfuse

```sh
python3 scripts/setup_langfuse.py
docker compose -f compose.langfuse.yaml up -d --wait --wait-timeout 300
docker compose -f compose.yaml -f compose.observability.yaml up --build -d web
# 앱: http://localhost:8000 / 관측: http://localhost:3000
```

호출별 모델·토큰·상태·지연·실제 비용을 기록한다. 프롬프트·응답 본문·키는 보내지 않는다. 생성 실패도 비용에 포함하며 관측 장애로 모델을 다시 부르지 않는다. Langfuse의 월 인프라 비용은 아직 실측하지 않았다. [설치와 비용 대사](docs/langfuse.md).

## 문서

- [코드 탐색 wiki](llm-wiki/index.md): 모듈 책임, 수정 지점, 영역별 `index.yaml`.
- [에이전트 설계](docs/agent-design.md): 후보 선정, 실행·측정 계약, 비용 예약과 한계.
- [비용 절감 결과](docs/cost-optimization-results.md): 채택 설정과 제외한 설정의 실제 비교.
- [이전 1회 경로 검증](docs/product-v2-validation.md): 세 실패 사례의 수정 이력.
- [Jev 검토](docs/jev-integration.md): 별도 평가 경로의 설계. 기본 1회 흐름에는 추가 평가 호출이 없다.

`--deep-review`로 실행하는 기존 다중 호출 CLI는 별도 실험 경로다. 운영 1회 경로의 비용·품질과 합산해 설명하지 않는다.
