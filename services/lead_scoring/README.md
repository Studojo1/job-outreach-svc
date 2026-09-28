# Lead Scoring Service

## Purpose
Evaluates lead quality and relevance to the candidate's profile using AI-driven analysis.

## Core Files
- `lead_scoring_service.py`: the live scorer. Weighted heuristics per lead (title, department, industry, seniority fit, location) plus LLM company-fit on the top leads. Seniority fit compares the lead's title with the candidate's level from the quiz (`_score_seniority_fit`).
- `llm_justifier.py`: plain-language reasons for top leads.
- `company_intelligence_service.py`: LLM company-fit scores.

## Inputs
Candidate profile and a list of leads.

## Outputs
Ranked leads with detailed score breakdowns (`overall_score`, `relevance` scores).

## External APIs Used
- Azure OpenAI for reasoning and scoring.
