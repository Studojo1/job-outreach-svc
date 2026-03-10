# Lead Calibration Service

## Purpose
Adaptively adjusts search filters to find the "Goldilocks zone" (not too many, not too few) of leads based on a target count.

## Core Files
- `filter_calibration_engine.py`: The primary feedback loop that tightens or loosens filters (geography, company size, seniority) until the lead count hits the target.
- `filter_generator_service.py`: Generates the initial seed filters based on a candidate's profile.

## Inputs
Initial `LeadFilter` and a `target_leads` integer.

## Outputs
A "calibrated" `LeadFilter` object and the expected total count.

## External APIs Used
- Apollo count-only searches.
