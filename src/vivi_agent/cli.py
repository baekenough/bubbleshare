"""CLI 입출력을 담당한다. 의사결정은 에이전트 그래프가 맡는다."""
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .evidence import EvidenceRepository
from .graph import AgentRunner
from .provider import ProviderError, create_provider, provider_api_key_name


ROOT = Path(__file__).resolve().parents[2]


def save_output(result, output):
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    else:
        print(rendered)


def main():
    parser = argparse.ArgumentParser(description="VIVI evidence-to-action LangGraph agent")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("analyze", "evidence"):
        p = sub.add_parser(command)
        p.add_argument("topic")
        p.add_argument("--snapshot", default="SNAP-2")
        p.add_argument("--context", type=Path)
        p.add_argument("--dataset", type=Path, default=ROOT / "VIVI_MVP_Sample_Dataset")
        p.add_argument("--output", type=Path)
        p.add_argument("--state-dir", type=Path, default=ROOT / ".vivi")
        p.add_argument("--model")
        p.add_argument("--provider", choices=("openai", "openrouter"))
        p.add_argument("--deep-review", action="store_true", help="Offline legacy 3–5 call quality review; not the production request path")
        p.add_argument("--customer", default="demo")
    p = sub.add_parser("evaluate")
    p.add_argument("--cases", type=Path, default=ROOT / "evals/action-cases.json")
    p.add_argument("--dataset", type=Path, default=ROOT / "VIVI_MVP_Sample_Dataset")
    p.add_argument("--output", type=Path, default=ROOT / "output/evaluation.json")
    p.add_argument("--state-dir", type=Path, default=ROOT / ".vivi")
    p.add_argument("--model")
    p.add_argument("--provider", choices=("openai", "openrouter"))
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    repo = EvidenceRepository(args.dataset)
    if args.command == "evidence":
        context = json.loads(args.context.read_text()) if args.context else None
        save_output(repo.build(args.topic, args.snapshot, context), args.output)
        return
    try:
        key_name = provider_api_key_name(args.provider)
    except ValueError as exc:
        parser.error(str(exc))
    if not os.environ.get(key_name):
        parser.error(f"{key_name} is required. Put it in the local .env or runtime environment.")
    provider = create_provider(args.provider, model=args.model, artifact_dir=args.state_dir / "traces")
    runner = AgentRunner(repo, provider, args.state_dir)
    try:
        if args.command == "evaluate":
            from .evaluation import evaluate
            result = evaluate(runner, json.loads(args.cases.read_text()))
        else:
            context = json.loads(args.context.read_text()) if args.context else None
            if args.deep_review:
                result = runner.run(args.topic, args.snapshot, context)
            else:
                from .product import ProductAgent
                from .store import ActionStore
                state_dir = args.state_dir / "product"
                result = ProductAgent(repo, provider, state_dir, ActionStore(state_dir / "actions.sqlite")).run(
                    args.customer, args.topic, args.snapshot, context)
        save_output(result, args.output)
        if args.output:
            usage = {k: v for k, v in (result.get("usage") or {}).items() if k != "by_run_key"}
            print(json.dumps({"output": str(args.output), "status": result.get("status"),
                              "decision": result.get("decision"), "cache_hit": result.get("cache_hit"),
                              "usage": usage}, ensure_ascii=False))
    except ProviderError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
