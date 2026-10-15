# 검증 범위와 변경 영향

기본 회귀 검사는 공급사 호출 없이 실행한다.

```sh
uv run pytest -q
npm run build --prefix frontend
```

CLI `analyze`와 별도 평가 스크립트는 설정된 키로 실제 호출할 수 있다. 오프라인 테스트와 실제 비용이 발생하는 품질 검증을 구분한다. `--deep-review`는 여러 번 호출하는 별도 경로이므로 운영 1회 결과의 대용으로 쓰지 않는다.

| 바꾼 내용 | 먼저 확인할 테스트 |
| --- | --- |
| 정제·집계·데이터 버전 | `test_evidence.py`, `test_analytics.py`, `test_relations.py` |
| 호출 분기·캐시·현재 액션 | `test_product_agent.py`, `test_runtime.py`, `test_store.py` |
| 후보 비교·구체 산출물·제품 사실 | `test_action_selection.py`, `test_output_constraints.py`, `test_integration_procedure.py` |
| 압축 입력·출력 확장 | `test_compact.py`, `test_context_references.py`, `test_asset_inventory.py` |
| 실행·게시·측정 조건 | `test_execution_policy.py`, `test_measurement_scope.py`, `test_rendered_measurement_graph.py` |
| 공급사 설정·실제 비용 | `test_provider_settings.py`, `test_actual_budget.py`, `test_observability.py` |
| HTTP 입력·저장·오류 | `test_api.py` |

파일은 모두 [tests](../../tests)에 있다. 스키마 검사에서는 ID, 타입, 질문 범위와 결정 규칙을 검증한다. 출력 문장에 그럴듯한 허위 기능이나 모순된 선정 이유가 남았는지는 별도 내용 검토가 필요하다.

## 고정 품질 사례

내용 검토는 근거·문제 적합성, 같은 문제의 후보 비교·선정, 실행 가능성, 사실·자산·게시 일관성, 측정의 5축을 각각 0~2점으로 평가한다. 기본 통과는 8점 이상, 모든 축 1점 이상, P1/P2 없음, 필수 회귀 충족이다. 비용 최적화 채택은 여기에 기존 사례별 9/9/10/9점 이상을 요구한다.

| 사례 | 반드시 확인할 내용 |
| --- | --- |
| lead | unknown 기능을 단정하지 않고 보조 SRC021을 편집·최종 인용 대상에서 제외 |
| unsupported | 확인된 미지원을 unknown으로 되돌리지 않고 수동·API 등 미확인 대안도 지원한다고 쓰지 않음 |
| security | 전체 후보에 같은 필수 기준을 모두 적용하고, 실패 후보 개정 금지·전체 검사 후 신규 작성 유지 |
| confirmed | 확인된 경로 A와 영수증, 입력·매핑·정상·중복·필수값 누락·대조 6단계, 실제 필드·정책은 운영자 확인 |

공통으로 원문 모델 응답과 계약을 붙인 최종 계획을 함께 읽는다. 코드가 정확한 규칙을 덧붙였어도 원문이 다른 실행을 지시하면 통과로 덮지 않는다. 게시 작업은 URL·자사 source ID·게시일시를 모두 확정하고, 주요 언급·추천과 인용 보조를 분리한다. 내부 작업은 게시 후 노출 대신 작업 완료·검증 근거를 평가한다.

채택한 GPT-5.4 low/표 압축 결과는 [독립 품질 기록](../../output/cost-table-gpt54-low/quality-audit.json)에 있다. 점수는 lead 9, unsupported 10, security 10, confirmed 9이며 P3 편집 한계 두 건이 남는다. 이 중 3건은 즉시 게시보다 내부 검증을 먼저 선정했다. 검사 표의 누락·기준 문구와 필수 필드 예시는 사람이 확인해야 한다.

같은 네 사례의 평균 생성 실비는 $0.185761875에서 $0.102391875로 44.88% 줄었다. 전체 비교 실험 20회 비용은 $1.14256725이며, 탈락한 설정의 비용도 포함한다. 설정별 수치와 해석은 [비용 최적화 결과](../../docs/cost-optimization-results.md)에 정리돼 있다.

GPT-5.4 none과 mini 설정의 실패도 [비용 집계](../../output/cost-optimization-costs.json)에 함께 남긴다. 스키마 4/4 통과만으로 모델 설정을 채택하지 않는다. 측정한 절감률은 같은 네 사례의 표본 결과이며 일반적인 모델 품질·실서비스 성공률을 입증하지 않는다.

코드·프롬프트 변경 후 이전 실제 응답을 재검증한 결과는 새 모델 호출과 구분한다. 재검증으로 구조 호환성은 볼 수 있지만 바뀐 프롬프트의 생성 품질을 입증할 수는 없다. 제공 데이터와 제품 사실 시험 맥락은 합성 자료이며 실제 제품 연동·게시·후속 노출 개선을 수행하지 않았다.
