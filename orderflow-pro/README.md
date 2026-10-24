# OrderFlow Pro — Render deployment

This folder is isolated on branch `orderflow-pro-render`. Render deploys `orderflow-pro/Dockerfile` with repository root as its build context.

## Dashboard

The dashboard is password-protected and uses HTTPS with an HTTP-only session cookie. `APP_SECRET_KEY` and `ORDERFLOW_ACCESS_PASSWORD` are already configured in the new Render service. Keep the password private and rotate it in Render if it is shared.

## Enable Angel One live feed

Add these four variables in the Render service's Environment tab, then save/redeploy:

- `ANGEL_API_KEY`
- `ANGEL_CLIENT_CODE`
- `ANGEL_PIN`
- `ANGEL_TOTP_SECRET` (the TOTP seed string, not a six-digit rotating code)

Do not put credentials in GitHub or send them in chat.

## Verify

`/health` must return HTTP 200 with `status: ok`. Live market data is confirmed only when `broker_login` and `feed_connected` are true and `last_tick_at` changes. If those fields are false, check the Render app logs and credentials.

## Data limitation

Footprint buy/sell estimates classify cumulative-volume increments using a price tick rule. This is not exchange-provided aggressor-side order flow or full bid/ask depth and should not be used as an automatic trade instruction.
