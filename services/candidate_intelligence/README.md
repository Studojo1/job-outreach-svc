# Candidate Intelligence Service

## Purpose
Extracts data from resumes and manages the AI conversational profiler to parse unstructured career history into a deterministic JSON object (`CandidateIntel`).

## Core Files
- `parser.py`: Fast, deterministic document parser using PyMuPDF and regex. Does not use LLMs to maximize upload speed.
- `profiler_agent.py`: LangChain/Azure OpenAI state machine interacting with candidates via chat.
- `_payload_builder.py` and `_question_flow.py`: Helper logic for the profiler's internal state.
- `career_ontology.py`: Source of truth for all role/seniority definitions.
- `models.py`: Pydantic definitions for states and outputs.

## Inputs
Files (`.pdf`, `.docx`) and simple User string messages.

## Outputs
Valid `CandidateIntel` schema containing location, target roles, seniority, and parsed background context.
