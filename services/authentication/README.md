# Authentication Service

## Purpose
Handles OAuth flows for third-party services, primarily Google Gmail.

## Core Files
- `auth_service.py`: Modular interface for connecting accounts and getting valid tokens.
- `google_oauth.py`: Clean, isolated Google OAuth handshake logic.
- `token_manager.py`: Securely handles `access_token` and `refresh_token` persistence and rotation.

## Inputs
User identifiers and OAuth callback codes.

## Outputs
Authenticated user sessions and valid API tokens.

## External APIs Used
- Google OAuth 2.0.
