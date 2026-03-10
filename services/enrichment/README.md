# Enrichment Service

## Purpose
Enriches lead data with verified email addresses and other missing professional details.

## Core Files
- `enrichment_service.py`: Manages the enrichment loop, interacting with data providers (Apollo fallback).

## Inputs
Lead objects with basic identifiers.

## Outputs
Leads with `email` and `phone` fields populated where possible.

## External APIs Used
- Apollo People Enrichment API.
