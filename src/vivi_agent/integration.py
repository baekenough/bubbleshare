"""확인된 기능과 아직 제공되지 않은 연동 설정을 구분한다."""

PHASES = {"input", "field_mapping", "normal_submission", "duplicate_submission",
          "missing_required_field", "reconcile"}


def verified_integration(context: dict) -> dict | None:
    route, receipt = context.get("verified_route"), context.get("evidence_receipt")
    if (context.get("verified_web_form_auto_registration") is True
            and isinstance(route, str) and route.strip()
            and isinstance(receipt, str) and receipt.strip()):
        return {"verified_route": route, "verification_ref": receipt}
    return None


def validate_integration(action: dict, pack: dict) -> list[str]:
    """확인된 경로와 검증 단계를 검사한다. 절차 내용의 타당성은 별도로 검토한다."""
    known = verified_integration(pack.get("business_context", {}))
    procedure = action.get("integration_procedure")
    if not known:
        return ["integration_procedure: no receipted supported route exists"] if procedure else []
    if not procedure:
        return ["integration_procedure: a concrete procedure for the verified route is required"]
    errors = []
    for field, expected in known.items():
        if procedure.get(field) != expected:
            errors.append(f"integration_procedure.{field}: must preserve the exact verified context value")
    steps = procedure.get("steps", [])
    missing = PHASES - {step.get("phase") for step in steps}
    if missing:
        errors.append("integration_procedure.steps: missing phases " + ", ".join(sorted(missing)))
    for i, step in enumerate(steps):
        for field in ("input_example", "operation", "expected_result", "verification_artifact"):
            if not str(step.get(field, "")).strip():
                errors.append(f"integration_procedure.steps[{i}].{field}: required")
    if not procedure.get("prerequisites_unknown"):
        errors.append("integration_procedure.prerequisites_unknown: unprovided configuration details must be identified")
    if len(procedure.get("release_checks", [])) < 2:
        errors.append("integration_procedure.release_checks: execution evidence and approval checks required")
    return errors
