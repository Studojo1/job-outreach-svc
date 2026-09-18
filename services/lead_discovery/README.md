# Lead Discovery Service

## Purpose
Interacts with the Apollo.io API to find potential leads based on search criteria. Handles pagination, rate limits, and title-chunking for complex queries.

## Core Files
- `apollo_service.py`: Direct API client for Apollo's people search.
- `apollo_query_builder.py`: Translates `LeadFilter` objects into Apollo-specific JSON query payloads.
- `lead_pool_collector.py`: Orchestrates the fetching of large lead pools in batches.
- `lead_collector_service.py`: Higher-level service for lead ingestion and parsing.

## Inputs
`LeadFilter` schemas, candidate context, and search parameters.

## Outputs
Lists of leads with `apollo_id`, name, contact info, and company details.

## External APIs Used
- Apollo "mixed_people/api_search" endpoint.
