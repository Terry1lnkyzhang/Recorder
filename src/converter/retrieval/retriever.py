from __future__ import annotations

from dataclasses import asdict

from .models import CandidateMatch, SemanticStep
from .scoring import score_method_candidate, score_script_candidate
from ..registry.models import RegistryBundle


class RegistryRetriever:
    def __init__(self, registry_bundle: RegistryBundle) -> None:
        self.registry_bundle = registry_bundle

    def retrieve_for_step(self, step: SemanticStep, top_k_methods: int = 5, top_k_scripts: int = 3) -> dict[str, list[CandidateMatch]]:
        method_matches: list[CandidateMatch] = []
        for entry in self.registry_bundle.methods.entries:
            score, reason = score_method_candidate(step, entry)
            if score <= 0:
                continue
            method_matches.append(
                CandidateMatch(
                    candidate_type="method",
                    name=entry.name,
                    score=score,
                    summary=entry.summary,
                    reason=reason,
                    payload=asdict(entry),
                )
            )

        script_matches: list[CandidateMatch] = []
        for entry in self.registry_bundle.scripts.entries:
            score, reason = score_script_candidate(step, entry)
            if score <= 0:
                continue
            script_matches.append(
                CandidateMatch(
                    candidate_type="script",
                    name=entry.name,
                    score=score,
                    summary=entry.summary,
                    reason=reason,
                    payload=asdict(entry),
                )
            )

        method_matches.sort(key=lambda item: item.score, reverse=True)
        script_matches.sort(key=lambda item: item.score, reverse=True)
        return {
            "methods": method_matches[:top_k_methods],
            "scripts": script_matches[:top_k_scripts],
        }

    def retrieve_for_steps(self, steps: list[SemanticStep], top_k_methods: int = 5, top_k_scripts: int = 3) -> dict[int, dict[str, list[CandidateMatch]]]:
        return {
            step.step_id: self.retrieve_for_step(step, top_k_methods=top_k_methods, top_k_scripts=top_k_scripts)
            for step in steps
        }