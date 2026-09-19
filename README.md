# HugCollect Bot

## Render variables

Required:
- `BOT_TOKEN`
- `SUPPORT_USERNAME`

Subscriptions:
- `WEEKLY_CHANNEL_ID` — private channel ID for weekly access
- `MONTHLY_CHANNEL_ID` — private channel ID for monthly access
- or `SUBSCRIPTION_CHANNEL_ID` for one common private channel
- `CRYPTO_PAY_API_TOKEN` — Crypto Pay API token for automatic CryptoBot verification
- `CRYPTO_ASSET` — default `USDT`

Optional fallback links (manual only, no automatic verification):
- `CRYPTO_FALLBACK_WEEK`
- `CRYPTO_FALLBACK_MONTH`

The bot must be an administrator of the subscription channel(s) with permission to invite and ban/restrict members.

Telegram Stars payments are handled by the bot and, after successful payment, the bot creates a one-use channel invite link. Telegram's native paid channel invite subscriptions currently support a 30-day period, so the requested 7-day plan is implemented as a one-time Stars payment plus 7-day bot entitlement rather than a native recurring channel subscription.
