AI package conventions:

- Put all AI-related features under `src/ai/`.
- Separate independent capabilities into focused modules or subpackages.
- Keep prompt building, transport/client, result parsing, and post-processing decoupled.
- Reuse common AI transport and error handling instead of duplicating them in feature modules.

Current layout:

- `client.py`: shared OpenAI-compatible transport client
- `errors.py`: shared AI exception types
- `models.py`: session-analysis result models
- `memory.py`: carry-over memory persistence for batch analysis
- `prompt_builder.py`: session-analysis prompt and media preparation
- `session_analyzer.py`: session-level AI workflow analysis
- `suggestions/`: reserved for AI-assisted conversion suggestions such as method selection and parameter recommendation

Current session-analysis architecture:

- Stage 1: step-level analysis. Each request focuses on one small batch of steps and their screenshots, producing `step_insights` plus lightweight carry-over memory.
- Stage 2: workflow aggregation. A second AI call consumes only the accumulated `step_insights` and carry memory, then outputs `invalid_steps`, `reusable_modules`, `wait_suggestions`, and workflow notes.
- Coverage analysis remains a separate AI call in the viewer and uses the summarized workflow text rather than replaying all screenshots.

Recommended future AI modules:

- `suggestions/method_selection.py`
- `suggestions/parameter_recommendation.py`
- `suggestions/prompt_builder.py`
- `suggestions/models.py`
- `suggestions/service.py`

Rule of thumb:

- If a feature calls an LLM or consumes LLM output, it belongs under `src/ai/`.
- If a feature is deterministic retrieval/scoring without AI, keep it outside `src/ai/`.