# Shared Service

## Purpose
Contains universal utilities shared across multiple domains to prevent duplicated logic or circular dependencies.

## Structure
- `azure_client.py`: A wrapper client connecting to Azure OpenAI that encapsulates API key management, payload structuring, HTTP retries, and strict JSON Schema validation.

## Inputs
Typically takes raw prompt strings and structured JSON schemas from higher-level services.

## Outputs
Returns deterministic, pre-validated python dictionaries parsed from Azure OpenAI's raw text response.

## External APIs Used
- Azure OpenAI Chat Completions endpoint.
