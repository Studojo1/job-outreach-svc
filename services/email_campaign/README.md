# Email Campaign Service

## Purpose
Manages the lifecycle of an email outreach campaign, from template composition to delivery.

## Core Files
- `campaign_service.py`: High-level campaign management (create, schedule, monitor).
- `gmail_service.py`: Standardized Gmail interaction wrapper.
- `gmail_send_service.py`: Low-level logic for sending individual emails via SMTP/API.

## Inputs
Campaign templates (subject, body), lead lists, and Gmail credentials.

## Outputs
Campaign status updates and `emails_sent` audit logs.

## External APIs Used
- Google Gmail API.
