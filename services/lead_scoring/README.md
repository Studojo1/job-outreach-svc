# Lead Scoring Service

## Purpose
Evaluates lead quality and relevance to the candidate's profile using AI-driven analysis.

## Core Files
- `lead_scoring_service.py`: Higher-level logic for scoring lead batches.
- `lead_scoring_engine.py`: The LLM-driven core that analyzes title, company, and seniority against candidate preferences.

## Inputs
Candidate profile and a list of leads.

## Outputs
Ranked leads with detailed score breakdowns (`overall_score`, `relevance` scores).

## External APIs Used
- Azure OpenAI for reasoning and scoring.
