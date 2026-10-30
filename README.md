# AI-Powered Multimodal Interview Performance Analyser

## Structured Evaluation Layer

The Interview Analyzer uses a local structured evaluation layer alongside the existing LLM-based NLP evaluator.

The structured evaluator provides deterministic signals such as:
- technical correctness
- relevance
- completeness
- misconception detection
- follow-up requirement

It runs locally and does not require a separate paid API.

### Architecture & Scoring
- **Primary LLM**: Groq generates detailed natural-language feedback, qualitative strengths, knowledge gaps, and overall assessments.
- **Secondary Structured Layer**: `LocalStructuredEvaluator` runs explainable, deterministic Python evaluation on the candidate's answer to generate structured signals without incurring any cloud or API costs.
- **Combined NLP Score**: When structured evaluation is enabled, the composite NLP score is computed as:
  $$\text{combined\_nlp\_score} = \text{round}(0.75 \times \text{overall\_nlp\_score} + 0.25 \times \text{structured\_score})$$
  If structured evaluation is disabled or encounters an issue, it safely defaults to `overall_nlp_score` without interrupting the interview.

### Configuration
In `.env`:
```env
STRUCTURED_EVALUATION_ENABLED=true
STRUCTURED_EVALUATION_WEIGHT=0.25
```
cd D:\projects\interview-analyzer
.\myenv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000