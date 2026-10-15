from pathlib import Path

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.graph import render_metric_facts


def test_model_cannot_merge_mentions_and_citation_rates_in_quantitative_facts():
    pack = EvidenceRepository(Path(__file__).resolve().parents[1] / "VIVI_MVP_Sample_Dataset").build("lead")
    diagnosis = {"facts": [{"statement": "Gemini 언급과 인용 모두 3/5",
                            "evidence_ids": [], "metric_ids": ["channel.Gemini.target_mentions",
                                                               "channel.Gemini.target_owned_citations"]}]}
    result = render_metric_facts(diagnosis, pack)
    assert "channel.Gemini.target_mentions: 3/5" in result["facts"][0]["statement"]
    assert "channel.Gemini.target_owned_citations: 1/5" in result["facts"][0]["statement"]
    assert diagnosis["facts"][0]["statement"] == "Gemini 언급과 인용 모두 3/5"
