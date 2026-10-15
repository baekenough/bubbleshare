# 측정 범위와 연동 절차 수정·재검증

2026-10-14 · 프롬프트 v8 · OpenRouter `openai/gpt-5.4` · reasoning medium

이전 평가에서 실패한 기능 미지원·지원 확인 두 사례를 수정했다. 원래 `evals/action-cases.json`의 데이터·기능 맥락·기대 조건을 그대로 사용했다. 두 사례만 추린 `evals/failure-regression.json`도 원본과 동일함을 확인했다. 커밋은 하지 않았다.

## 무엇을 고쳤는가

### 1. 측정 범위와 문장을 하나의 계약에서 생성

주요 지표는 액션의 질문·채널 안에 있는 `question.*` 지표다. 주제·채널 전체 지표는 보조 지표로 분리한다. `compile_measurement`가 실제 관측 ID, 분자·분모, 질문 문장·언어·채널을 검증하고 정확한 후속 수집 목록을 만든다.

예를 들어 Q068의 3채널과 Perplexity의 리드 관리 5질문을 선택하면 중복을 제외한 **7페어**를 수집한다. 게시 확인일로부터 **7일·14일 후 각각 1회**, 총 14개 응답이다. 각 지표는 원래 고정 분모로 비교하며, 주요 지표가 개선돼야 긍정 신호다. 보조 지표의 개선만으로 성공 처리하지 않는다.

`partial`, `failed`, 언어 불일치와 누락은 미언급 0이 아니다. 고정 페어가 빠진 지표는 `insufficient_data`로 처리하고 재수집한다. 신규 자산의 URL·source_id 등록 또는 기존 자산의 최종 선택이 완료되어야 후속 수집을 시작한다.

최종 `follow_up`, `positive_signal`, `stop_or_revise`, `monitoring_plan`도 코드 계약에서 생성한다. 모델이 별도로 설명하면서 분모·결측 조건을 생략하는 문제를 제거했다. 검토와 평가에는 같은 계약·문장을 전달하고, 모델이 원래 쓴 응답은 원장에 보존한다.

### 2. 확인된 경로를 실제로 따라갈 수 있는 연동 절차

`integration_procedure`는 확인된 경로명·확인 기록을 정확히 보존한다. 입력 준비 → 필드 매핑 → 정상 제출 → 중복 제출 → 필수값 누락 → 결과 대조의 **6단계**마다 입력 예시·작업·기대 결과·남길 증빙을 요구한다.

실제 메뉴/API 주소, 필드 식별자, 권한, 중복·필수값 정책은 제공되지 않았다. 이 값들은 담당자가 확인할 선행 조건으로 남긴다. 알려진 경로를 다시 확인 대기 상태로 돌리거나 미확인 동작을 지원한다고 만들어내지 않는다. 무료 체험도 unknown으로 유지한다.

### 3. 기존 자산 검토 누락 방지

전체 회귀에서 전문 가이드 SRC022를 근거로 언급하면서도 재사용 후보 판단에서는 빠뜨리는 문제를 추가로 발견했다. 이제 자사 자산 6개 모두에 재사용 점검 또는 이번 작업에서 제외하는 이유가 필요하다. 점검 후보와 `source_ids`가 다르면 코드가 차단한다. 본문을 제공받지 않았으므로 실제 내용을 단정하지 않고 조건부 개정/신규 작성 규칙을 사용한다.

## 검증 결과

실패했던 두 사례는 **2/2 통과**, 전체 회귀는 **7/8 통과**했다. 미지원 사례는 수정·재검토를 포함해 5회, 지원 확인 사례는 3회 모델 호출로 완료됐다. 별도 수용 평가도 두 사례 모두 통과했고, 추가 읽기 전용 검토에서 새 P1/P2 결함은 발견되지 않았다([검토 기록](../output/independent-audit-v8.json)).

| 사례 | 결과 |
| --- | --- |
| `lead_actionable_brief` | 통과 |
| `automation_observe` | 통과 |
| `scale_missing_observations` | 통과 |
| `security_existing_asset` | 실패 — review_required |
| `lead_capability_unsupported` | 통과 |
| `reject_generic_action` | 통과 |
| `reject_nonexistent_evidence` | 통과 |
| `lead_capability_confirmed` | 통과 |

**남은 보안 사례:** `proposed_title`과 예시 제목이 OrbitCRM을 “국내 CRM”으로 분류하지만 확인된 제품 근거가 없다. 수정·재검토 5회 한도 후에도 남아 승인하지 않았다. 해결하려면 제목의 확정 표현을 중립화하거나 확인 근거를 제공해야 한다. 두 요청 대상의 수정 완료와 별개로, 전체 목표의 “금지된 주장 0개” 조건은 아직 달성하지 못했다. 실패한 보안 초안을 성공 추천으로 내보내지 않는다.

미지원 최종 액션은 자동 등록 미지원 범위를 명확히 안내하는 문서 정비다. 확인되지 않은 수기 등록·대체 연동을 약속하지 않으며 **Q068 3페어 × 2회 = 6개 응답**을 관측한다. 지원 확인 액션은 실제 경로 A의 단계별 안내·시험표를 만들고 **Q068 주요 3페어 + Perplexity 보조 5페어의 합집합 7페어 × 2회 = 14개 응답**을 관측한다.

[호출·비용 기록](../output/runtime-usage-v8.json): 최종 전체 회귀는 32회, OpenRouter 보고 비용 **$2.987910**이다. 두 사례 전용 평가와 공유한 생성 호출의 중복을 제거하면 최종 검증 합계는 **34회 / $3.091588**이다. 중간 수정본까지 포함한 로컬 OpenRouter 호출 전체는 70회 / $6.742284이며 미완료 예약은 없다. 과제 환산 원가와 실제 공급자 보고 비용은 분리했다.

코드 테스트는 **182개 및 하위 테스트 18개 통과**했다. 주요/보조 분모, 결측과 변화 없음, 보조만 개선, 연동 경로·확인 기록·6단계, 자산 목록 누락, 정상/수정 경로의 렌더링과 원문 보존, OpenRouter 전송·오류·사용량 처리를 검사했다.

Docker 빌드와 컨테이너 실행이 통과했다. [Docker 결과](../output/confirmed.v8.docker.json)는 호스트의 지원 확인 사례와 plan·execution_contracts가 동일하고 `cache_hit=true`다. 분석 호출은 3회 그대로 유지됐으며 Docker 재실행의 추가 호출은 0회다.

- [기능 미지원 사례의 최종 액션](../output/unsupported-action-v8.md)
- [지원 확인 사례의 최종 연동 절차·측정표](../output/confirmed-integration-v8.md)
- [일반 리드 관리 추천](../output/lead-action-v8.md)
- [두 실패 사례의 수용 평가](../output/evaluation.focused-v8.json)
- [전체 8개 회귀 평가](../output/evaluation.regression-v8.json)

### 중간 실패도 보존

v7의 최초 두 사례 평가는 2/2였지만, 추가 검토에서 미지원 사례의 자산 선택 체크표에 모순이 발견됐다. 피드백을 체크포인트에 넣어 남은 수정·재검토 2회를 사용했으나 측정 문구의 조건 누락 때문에 최종 `review_required`가 됐다. 전체 v7 결과는 **6/8**이었다. 최초 2/2를 최종 성공으로 보고하지 않는다.

이 결과와 [추가 검토 피드백](../output/unsupported-independent-review-v7.json), [v7 전체 평가](../output/evaluation.regression-v7.json)를 보존했다. v8은 위 구조 변경 후 새 캐시 키로 재생성한 결과이며, 수작업으로 승인 상태나 생성된 액션을 바꾸지 않았다.

## 재현

로컬 `.env`에 `OPENROUTER_API_KEY`, `VIVI_PROVIDER=openrouter`, `OPENROUTER_MODEL=openai/gpt-5.4`를 설정한다. 키는 Git과 Docker 빌드에서 제외하고 실행 시에만 주입한다. OpenAI 직접 호출 경로도 유지하며 제공자가 다르면 캐시가 분리된다.

```sh
uv sync --frozen
uv run pytest -q
uv run vivi-agent evaluate --provider openrouter --cases evals/failure-regression.json --output output/evaluation.focused-v8.json
uv run vivi-agent evaluate --provider openrouter --output output/evaluation.regression-v8.json
uv run vivi-agent analyze lead --provider openrouter --context evals/contexts/lead_capability_confirmed.json --output output/lead_capability_confirmed.v8.json
docker compose build agent
docker compose run --rm agent evaluate --provider openrouter --cases /app/evals/failure-regression.json --output /app/output/evaluation.focused-docker.json
```

Docker 기본 볼륨과 호스트 `.vivi`는 별개다. 기본 Docker 명령의 첫 실행은 새 호출을 사용하며 이후 같은 데이터·코드·모델·맥락은 재사용한다. 위 Docker 재사용 검증은 호스트 `.vivi`를 명시적으로 마운트해 수행했다.

OpenRouter 어댑터는 공식 [구조화 출력](https://openrouter.ai/docs/guides/features/structured-outputs), [제공자 선택](https://openrouter.ai/docs/guides/routing/provider-selection), [추론 토큰](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), [사용량](https://openrouter.ai/docs/cookbook/administration/usage-accounting) 문서를 기준으로 구현했다. SDK 자동 재시도와 제공자 fallback을 끄고, 응답의 `usage.cost`를 실제 공급자 보고 비용으로 보존한다. 과제의 호출당 $0.018 환산 원가와 구별한다.

## 확인한 범위

제공 데이터는 과제용 합성 데이터다. 이번 검증은 **제공 원본 데이터와 실제 모델 API를 사용한 실행·품질 평가**다. 실제 제품에 연결해 연동을 수행하거나, 문서를 게시하거나, 노출 상승을 측정한 것은 아니다. 기능 지원/미지원 맥락도 평가용 fixture다. 생성·검토·평가가 같은 모델을 사용하므로 오류가 상관될 수 있다.
