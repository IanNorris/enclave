"""Tests for the Fusion judge/synthesizer prompts.

Covers two review P0s:
- fusion-blind-judge: the judge/synthesizer never see which model produced a
  response (self-preference defence).
- fusion-consensus-verdict: consensus is framed as signal not confidence, a
  disagreement section is elevated, and a settled/contested/unresolved verdict
  can escalate to a human.
"""

from enclave.common.fusion import build_judge_prompt, build_synthesizer_prompt

_RESPONSES = [
    ("claude-opus-4.8", "Alpha says X"),
    ("gpt-5.5", "Beta says Y"),
    ("gemini-3.1-pro-preview", "Gamma says Z"),
]
_MODEL_IDS = [label for label, _ in _RESPONSES]


class TestBlindJudge:
    def test_judge_prompt_hides_model_identity(self):
        p = build_judge_prompt("Question?", _RESPONSES)
        assert "(from " not in p
        for mid in _MODEL_IDS:
            assert mid not in p, f"judge prompt leaks model id {mid}"

    def test_synthesizer_prompt_hides_model_identity(self):
        p = build_synthesizer_prompt("Question?", "analysis", _RESPONSES)
        assert "(from " not in p
        for mid in _MODEL_IDS:
            assert mid not in p, f"synthesizer prompt leaks model id {mid}"

    def test_responses_labelled_by_position(self):
        p = build_judge_prompt("Question?", _RESPONSES)
        assert "### Response 1" in p
        assert "### Response 2" in p
        assert "### Response 3" in p

    def test_response_bodies_are_preserved(self):
        # Blinding removes the label, not the content.
        p = build_judge_prompt("Question?", _RESPONSES)
        assert "Alpha says X" in p and "Beta says Y" in p and "Gamma says Z" in p


class TestConsensusVerdict:
    def test_judge_does_not_frame_agreement_as_high_confidence(self):
        p = build_judge_prompt("Question?", _RESPONSES)
        assert "high-confidence" not in p

    def test_judge_elevates_disagreement(self):
        p = build_judge_prompt("Question?", _RESPONSES)
        assert "Disagreement" in p
        assert "highest-signal" in p

    def test_judge_emits_a_verdict(self):
        p = build_judge_prompt("Question?", _RESPONSES)
        assert "Verdict" in p
        for word in ("settled", "contested", "unresolved"):
            assert word in p

    def test_synthesizer_orders_by_priority_not_origin(self):
        p = build_synthesizer_prompt("Question?", "analysis", _RESPONSES)
        assert "Lead with the consensus" not in p
        assert "IMPORTANCE" in p

    def test_synthesizer_escalates_unresolved_to_human(self):
        p = build_synthesizer_prompt("Question?", "analysis", _RESPONSES)
        assert "human judgement" in p
