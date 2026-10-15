# 데이터와 상태 저장

[VIVI_MVP_Sample_Dataset](../../VIVI_MVP_Sample_Dataset)의 JSON이 분석 원본이다. [EvidenceRepository](../../src/vivi_agent/evidence.py)는 원본 바이트로 버전 해시를 만들고 질문·응답·브랜드 언급·인용·출처를 묶는다. 외부 사이트 본문을 수집하거나 기능 사실을 확인하는 모듈은 아니다.

관측은 질문·채널·수집 회차로 묶는다. 완전 수집, 질문 언어 일치, 최신 시각, 관측 ID 순서로 대표 응답을 고른다. 대표가 아닌 시도는 `superseded`로 남기며 `partial`, `failed`, 언어 불일치는 지표에서 제외한다. 한 응답에 언급이나 인용 행이 여러 개 있어도 응답 수는 한 번만 센다.

브랜드 언급, 추천 맥락의 언급, 자사 출처 인용은 서로 다른 지표다. 자사 페이지를 인용했어도 브랜드를 언급하지 않을 수 있다. 이전 회차 비교도 같은 질문/채널의 적격 응답으로 맞춘다. 빠진 응답은 미노출 0이 아니다.

| 저장 수단 | 내용 | 원본과의 관계 |
| --- | --- | --- |
| [DuckDBAnalytics](../../src/vivi_agent/analytics.py) | 응답 단위 지표 집계 | JSON에서 다시 만들 수 있는 분석 파일 |
| [RelationProjection](../../src/vivi_agent/relations.py) | 질문→응답→인용 출처→소유 브랜드, 응답→언급 브랜드 | JSON에서 다시 만들 수 있는 LatticeDB 파일 |
| [ActionStore](../../src/vivi_agent/store.py) | 제안, 사용자 액션, 수정·상태 변경 이력 | 사용자 상태의 영속 저장소 |
| [Ledger](../../src/vivi_agent/ledger.py) | 과제 단가 호출 예약·결과·실패 | 고객별 SQLite 원장 |
| [ActualBudget](../../src/vivi_agent/actual_budget.py) | 실제 달러 예약·정산·미확인 과금 | 고객별 별도 SQLite 원장 |
| JSON / JSONL | 정확한 초안 캐시, 실행·변경 이벤트 내보내기 | 캐시는 재검증하며 이벤트는 식별자로 중복 복구 |

DuckDB와 LatticeDB는 같은 데이터 버전을 사용한다. 버전이 다르면 섞어 읽지 않는다. DuckDB 집계는 정제 근거의 분자·분모와 일치하는지도 검사한다. LatticeDB는 로컬 파일로 열고, 열기부터 조회·닫기까지 파일 잠금을 유지한다. 벡터 저장이나 임베딩 API는 사용하지 않는다.

질문 우선순위는 `중요도 × 추천 공백 × 유효 수집률`이다. 매출 기대값이나 인과 효과 추정치가 아니다. 계산은 [analytics.py의 priorities](../../src/vivi_agent/analytics.py)에 있으며 모델이 순위를 다시 정하지 않는다.

사용자 상태는 `draft`, `in_progress`, `done`, `dismissed`다. 활성 과업 중복은 저장 트랜잭션 안에서 다시 검사한다. 완료된 과업도 같은 데이터 버전에서는 재추천에서 제외한다. `done`이나 저장 여부는 실제 노출 개선을 뜻하지 않는다.

고객 범위는 API 서버의 `VIVI_CUSTOMER_ID`로 고정된다. 클라이언트가 다른 고객을 지정하는 요청은 거절한다. 현재는 인증·조직 관리가 없는 로컬 단일 고객 앱이다.

변경 영향: 정제 순서나 분모를 바꾸면 `test_evidence.py`, `test_analytics.py`, `test_measurement_scope.py`를 함께 본다. 원본 파일을 바꾸면 버전·분석 파일·관계 파일·초안 캐시가 함께 달라진다. 사용자 액션 DB와 원장을 재생성 가능한 분석 파일처럼 삭제하지 않는다.
