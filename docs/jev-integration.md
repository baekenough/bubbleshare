# Jev를 액션 에이전트에 연결하는 경계

2026-10-14 공식 문서 확인 기준의 **선택적 통합 설계**다. TypeSafe 인증 키가 제공되지 않아 Jev API 호출과 한국어 추천 품질은 검증하지 않았다.

2026-10-15 운영 설계는 **모델 최대 1회**이며, 현재 실제 검증은 OpenRouter의 OpenAI 모델을 사용한다. 아래 다중 호출 검토 흐름과 5회 한도는 별도 오프라인 실험 경로의 설계다. 운영 요청에 Jev 검토를 추가하지 않는다. 도입하려면 오프라인 비교 후 기존 호출을 대체할지, 명시적인 별도 평가로 유지할지 결정해야 한다. 현재 구현 흐름은 [에이전트 설계](agent-design.md)를 따른다.

## 역할

Jev는 입력 상태에 대해 `Choice`, `Score`, `Noul` 형태의 판단을 반환한다. 자유로운 액션 초안·수정안·검토 설명 생성은 생성 모델이 담당한다. 따라서 우선 연결할 위치는 **작성된 ActionBrief의 개별 품질 조건을 판단하는 검토 보조 노드**다. [Introduction](https://docs.typesafe.ai/introduction)

```text
근거 집계(코드) → 진단(OpenAI) → 액션 작성(OpenAI) → 구조·참조 검사(코드)
                                                  ↓
                               원자적 품질 판단(Jev, 선택적)
                                                  ↓
                         의미 검토·수정 사유(OpenAI) → 통과/수정/보류
```

초기에는 같은 후보에 대한 Jev 결과를 비교 기록하는 `shadow` 모드로 사용한다. Jev의 판단으로 기존 검토 결과를 덮어쓰거나 필수 위반을 해제하지 않는다. 검증 후 어떤 검토 작업을 대체할지는 항목별로 결정한다. 이는 프로젝트의 통합 정책이다.

| 판단 단위 | 타입 | 입력과 출력 활용 |
| --- | --- | --- |
| 특정 관측이 특정 사실 주장을 지지하는가 | Choice | 해당 주장·원문·필요한 맥락만 전달; `supported / contradicted / insufficient` |
| 미확인 제품 기능을 확정적으로 주장하는가 | Noul | 초안의 기능 주장과 확인된 제품 사실; 위반의 yes 확률 |
| 특정 미확인 기능이 관련 게시 단계의 선행 확인으로 명시됐는가 | Noul | 한 기능·연관 단계·선행 조건; 충족의 yes 확률 |
| 담당자가 바로 만들 수 있는 산출물 내용이 있는가 | Score | 하나의 산출물; `추상적 권고 / 작업은 알지만 초안 부족 / 초안과 완료 조건 구체적` |
| 제작 완료와 후속 효과 측정을 구분했는가 | Score | 측정 계획과 기준점; 기존 0~2 루브릭의 이 항목만 전달 |

근거 ID 존재, 인용 문자열 일치, 분자·분모, 날짜·기간, 합계, 필수 필드 검사는 코드가 한다. TypeSafe의 [인용 검사 예제](https://docs.typesafe.ai/cookbooks/citation_check)도 문자열 검사를 먼저 하고 의미 적합성을 모델에 묻는다. `Score`는 수준별 확률의 가중 평균이므로 정수 평가 점수나 액션 성공 확률로 그대로 바꾸지 않는다. [Score](https://docs.typesafe.ai/primitives/score)

## Optional DecisionProvider 계약

아래는 구현 목표 인터페이스이며 현재 런타임 지원을 뜻하지 않는다. 생성용 `generate(stage, prompt, schema)`와 분리한다.

```text
evaluate(DecisionRequest) -> DecisionResult

DecisionRequest
  state: 근거 원문, 확인된 사실, 후보의 필요한 필드
  questions: {question_id: ChoiceQuestion | ScoreQuestion | NoulQuestion}
  model: 고정된 모델 버전
  policy_version, rubric_version, evidence_hash, candidate_hash

DecisionResult
  status: completed | unavailable | failed | invalid
  answers: {question_id: ChoiceAnswer | ScoreAnswer | NoulAnswer}
  requested_model, returned_model
  usage: input_tokens, output_tokens
  attempt_id, latency_ms, request_hash, failure_code
  assignment_tier, assignment_cost_usd
  vendor_estimated_cost_usd: number | null
```

응답은 요청한 모든 질문 키·타입·후보 집합을 검사한다. 확률은 유한한 0~1 값인지, 분포 합과 Score 범위가 맞는지 확인한다. 외부 응답이 유효하지 않으면 `invalid`로 기록한다. 판정 사유는 검사 항목과 참조 필드로 표현하고 Jev가 생성하지 않은 설명을 Jev의 발언처럼 붙이지 않는다.

## API와 실패 처리

- `POST https://api.typesafe.ai/v1/systemone`, Bearer 인증. `.env`의 별도 `TYPESAFE_API_KEY`를 사용한다. OpenAI 키를 재사용하지 않는다.
- 요청은 `state`, `model`, `questions`; 질문은 `type`, `instructions`, 타입별 `criteria`를 갖는다. 질문 ID 자체는 추론 입력이 아니므로 검사 대상과 조건은 `instructions`에 쓴다.
- Choice는 최대 255개 선택지, Score는 2~10개 수준. Noul 응답은 `noul` 확률이며 별도 `confidence` 필드가 없다.
- 응답의 `model`, `answers`, `usage`를 저장한다. 401·422는 설정/요청 오류로 종료하고, 429·529는 재시도 가능한 실패로 구분한다. [API reference](https://docs.typesafe.ai/api)

재시도는 그래프의 호출 원장에 먼저 예약하고 수행한다. SDK 사용 시 `RetryPolicy(max_retries=0)`으로 숨은 자동 재시도를 없앤다. 연결 실패·타임아웃의 과금 여부가 불명확하면 시도는 소비된 것으로 기록한다. [Retries](https://docs.typesafe.ai/sdk/python/api/retries)

키 미설정은 `unavailable`이며 호출 비용을 발생시키지 않는다. `shadow` 실패는 OpenAI 검토 경로를 계속 진행한다. 향후 Jev가 검토를 대체하는 모드에서 실패·낮은 신뢰도가 발생하면 남은 예산 안에서 OpenAI 검토로 대체하고, 예산이 없으면 `review_required`로 종료한다. 실패를 통과로 바꾸지 않는다.

기존 분석당 최대 5회 시도 한도에 Jev 호출과 fallback도 포함한다. 기본 OpenAI 3회에 Jev 1회를 추가하면 수정·재검토 2회를 모두 수행할 여유가 없어진다. 따라서 최초 Jev 비교 실험은 별도 오프라인 평가 예산으로 명시하고, 운영 도입 시 대체 노드·호출 순서·한도를 다시 설계한다.

## 모델·비용·검증 조건

공식 모델 문서의 현재 버전은 `jev-1.13.0`이다. 입력 $0.042/백만 토큰, 출력 과금 없음이며, 텍스트 입력만 지원한다. 요청 전체 64k, `state`와 가장 긴 질문 합계 32k 토큰 한도다. 영어가 주 학습 언어이고 한국어를 포함한 CJK 성능은 별도 검증이 필요하다. 평가는 별칭 대신 버전을 고정한다. [Models](https://docs.typesafe.ai/models)

과제 원가는 공급사 요금과 분리한다. [고정 단가 규칙](../VIVI_MVP_Sample_Dataset/pricing_assumptions.json)에 따라 Jev도 추론 요청마다 AI-S/M/L 중 하나로 센다. **액션 의미 검토는 보수적으로 AI-M 1회($0.018)**로 잡는다. 단순 응답 분류만 AI-S로 분리하는 비교안을 만들 수 있지만, 이때 가정 정확도는 S 94%, M 97%이며 Jev 실측치가 아니다. 의미 검토를 S 분류 성능으로 정당화하지 않는다. 공급사 추정 금액은 실제 `usage.input_tokens × 0.042 / 1,000,000`으로 별도 기록하며 무료 티어·Mock을 원가 0의 근거로 삼지 않는다.

공식 `confidence`는 출력 분포로 계산한 통계량이다. 0.9를 해당 액션의 성공률 90%나 한국어 도메인 정확도 90%로 읽지 않는다. 임계값은 실제 데이터에서 정하고 버전·문항·루브릭 변경 시 재평가한다. [Confidence](https://docs.typesafe.ai/confidence)

Jev 1.13은 숫자 계산, 간접 추론, 불필요하게 긴 상태, 적대적 입력, 선택지 순서에 알려진 한계가 있다. 따라서 주장별로 필요한 원문만 전달하고 원문을 지시로 취급하지 않도록 명시한다. 합산·정렬은 코드에서 수행한다. [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

첫 비교 평가는 실제 OpenAI 초안과 사람이 라벨링한 오류 변형을 같은 조건으로 평가한다. 기존 8개 평가 사례에 추상적 작업, 미확인 기능 단정, 누락된 게시 조건, 반대 근거 무시, 프롬프트 주입, 선택지 순서 변경을 포함한다. 필수 위반 탐지율·잘못된 통과·불필요한 보류·한국어 항목별 일치도·지연·실제 시도 수를 보고한다. 소수 사례만으로 보정된 확률이나 전반적 품질 우위를 주장하지 않는다.
