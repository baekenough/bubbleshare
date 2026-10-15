"""코드로 확정한 측정 범위를 포함해 저장된 에이전트 실행 결과를 렌더링한다."""
import argparse
import json
from pathlib import Path


def render(run):
    plan = run.get("plan") or {}
    lines = ["# " + plan.get("summary", "에이전트 실행 결과"), "",
             f"상태: `{run['status']}` · 결정: `{run['decision']}` · 모델: `{run['model']}`", "",
             "제공 데이터와 실제 API로 생성한 작업 초안이다. 제품 연동 실행이나 게시·후속 수집을 완료했다는 뜻이 아니다.", ""]
    for index, action in enumerate(plan.get("actions", [])):
        lines += ["## " + action["title"], "", action["opportunity"], "",
                  f"담당 역할 제안: {action['owner_role']} · 준비 상태: `{action['readiness']}`", "",
                  "### 선행 조건", "", *["- " + x for x in action["prerequisites"]], "",
                  "### 자산 선택", "", action["target_asset"]["selection_rule"], ""]
        for assessment in action["target_asset"].get("assessments", []):
            decision = "재사용 후보" if assessment["reuse_decision"] == "inspect_for_reuse" else "이번 작업에서 제외"
            lines += [f"- `{assessment['source_id']}` · {decision}: {assessment['rationale']}"]
        lines += [""]
        for deliverable in action["deliverables"]:
            lines += ["### " + deliverable["name"], "", deliverable["example_content"], ""]
        procedure = action.get("integration_procedure")
        if procedure:
            lines += ["### 검증된 연동 경로와 실행 절차", "",
                      f"확인된 경로: **{procedure['verified_route']}**", "",
                      f"확인 기록: `{procedure['verification_ref']}`", "",
                      "추가로 확인할 설정:", "", *["- " + x for x in procedure["prerequisites_unknown"]], ""]
            for n, step in enumerate(procedure["steps"], 1):
                lines += [f"#### {n}. {step['phase']}", "",
                          "입력: " + step["input_example"], "", "작업: " + step["operation"], "",
                          "기대 결과: " + step["expected_result"], "",
                          "남길 증빙: " + step["verification_artifact"], ""]
            lines += ["공개 전 통과 조건:", "", *["- " + x for x in procedure["release_checks"]], ""]
        lines += ["### 작업 순서", ""]
        lines += [f"{n}. {s['instruction']} 결과: {s['expected_output']}" for n, s in enumerate(action["steps"], 1)]
        lines += ["", "### 완료 조건", "", *["- " + x for x in action["acceptance_criteria"]], ""]
        for contract in run.get("execution_contracts", []):
            if contract["action_index"] != index:
                continue
            lines += ["### 코드로 확정한 측정 범위", "", "| 역할 | 지표 | 기준값 |", "| --- | --- | --- |"]
            for role in ("primary", "secondary"):
                for metric in contract[role + "_metrics"]:
                    lines += [f"| {'주요' if role == 'primary' else '보조'} | `{metric['metric_id']}` | {metric['numerator']}/{metric['denominator']} |"]
            cadence = contract["cadence"]
            lines += ["", f"게시 확인일 기준 7일·14일 후 각각 1회. 회차별 {cadence['pairs_per_round']}개 질문·채널 조합.", "",
                      "| 질문 | 채널 | 실제 수집 문장 |", "| --- | --- | --- |"]
            for sample in contract["sampling_plan"]:
                query = sample["query"].replace("|", "\\|").replace("\n", " ")
                lines += [f"| {sample['question_id']} | {sample['channel']} | {query} |"]
            lines += ["", "보조 지표의 변화만으로 주요 액션을 성공 처리하지 않는다. 결측은 미언급이 아니며, 동일한 유효 표본을 확보한 뒤 비교한다. 인과 효과는 입증되지 않았다.", "",
                      "추적 자산: " + ", ".join(contract["source_citation_check"]["source_ids"]), "",
                      "개정 자산은 최종 source_id를 선택하고, 새 자산은 게시 URL·source_id를 등록한 뒤 인용을 추적한다.", ""]
        lines += ["확정된 후속 수집 계획: " + action["measurement"]["follow_up"], "",
                  "긍정 신호: " + action["measurement"]["positive_signal"], "",
                  "중단·수정 조건: " + action["measurement"]["stop_or_revise"], ""]
    lines += ["## 한계", "", *["- " + x for x in plan.get("limitations", [])], ""]
    if run.get("validation_errors") or run.get("review_errors"):
        lines += ["검사 오류:", "", *["- " + x for x in run.get("validation_errors", []) + run.get("review_errors", [])], ""]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(render(json.loads(args.input.read_text())))
