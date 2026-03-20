# Discord Webhook End-to-End Verification

## Quick Start

This directory contains scripts to verify Discord webhook integration for trade notifications.

## Prerequisites

1. **Configure Discord Webhooks**: Add webhook URLs to your `.env` file
2. **Create Discord Webhooks**: Set up webhooks in your Discord server

## Verification Methods

### Method 1: Bash Script (Recommended - Fastest)

The bash script tests webhooks directly using curl, without starting the application.

```bash
# Test all configured webhooks
./scripts/test_discord_webhooks.sh
```

**What it does:**
- Loads Discord webhook URLs from `.env`
- Sends test notifications to each configured webhook
- Verifies HTTP response codes (204 = success)
- Reports pass/fail for each webhook

**Test notifications sent:**
- US Stocks: Buy order notification (green)
- KR Stocks: Sell order notification (red)
- Crypto: AI analysis notification (blue)
- Alerts: AI analysis notification (blue)

### Method 2: Python Script (Comprehensive)

The Python script tests the full TradeNotifier integration.

```bash
# Test all webhooks
uv run python scripts/test_discord_webhook_e2e.py

# Test specific webhook
uv run python scripts/test_discord_webhook_e2e.py --market-type crypto

# Dry run (see what would be sent)
uv run python scripts/test_discord_webhook_e2e.py --dry-run

# Verbose output
uv run python scripts/test_discord_webhook_e2e.py --verbose
```

**What it does:**
- Initializes TradeNotifier with webhook configuration
- Tests all notification types (buy, sell, analysis)
- Verifies embed structure and formatting
- More comprehensive testing than bash script

### Method 3: Manual curl Test

Test individual webhooks manually:

```bash
# Replace YOUR_WEBHOOK_URL with actual URL
curl -X POST "YOUR_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d '{
    "embeds": [{
      "title": "🧪 Manual Test",
      "description": "This is a manual test notification",
      "color": 65280,
      "fields": [
        {"name": "Test", "value": "Success!", "inline": true}
      ]
    }]
  }'
```

**Expected response:** Empty response with HTTP 204 (No Content)

## Expected Discord Messages

### Buy Notification (Green)
```
💰 매수 주문 접수
🕒 2026-03-06 14:30:00

┌─────────────────┬──────────────────┐
│ 종목            │ 비트코인 (BTC)   │
│ 시장            │ 암호화폐         │
│ 주문 수         │ 1건              │
│ 총 금액         │ 100,000원        │
│ 주문 상세       │ 1. 가격: ...     │
└─────────────────┴──────────────────┘
```

### Sell Notification (Red)
```
💸 매도 주문 접수
🕒 2026-03-06 14:30:00

┌─────────────────┬──────────────────┐
│ 종목            │ 이더리움 (ETH)   │
│ 시장            │ 암호화폐         │
│ 주문 수         │ 1건              │
│ 총 수량         │ 0.5              │
│ 예상 금액       │ 50,000원         │
└─────────────────┴──────────────────┘
```

### Analysis Notification (Blue)
```
📊 AI 분석 완료
🕒 2026-03-06 14:30:00

┌─────────────────┬──────────────────┐
│ 종목            │ 비트코인 (BTC)   │
│ 시장            │ 암호화폐         │
│ 판단            │ 📈 매수          │
│ 신뢰도          │ 85%              │
│ 주요 근거       │ 1. 상승 추세...  │
└─────────────────┴──────────────────┘
```

## Troubleshooting

### "No Discord webhooks configured"

**Cause:** Missing `DISCORD_WEBHOOK_*` variables in `.env`

**Solution:** Add webhook URLs to `.env`:
```bash
DISCORD_WEBHOOK_US=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_KR=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_CRYPTO=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_ALERTS=https://discord.com/api/webhooks/...
```

### "Webhook test failed (HTTP 400)"

**Cause:** Invalid webhook URL or webhook deleted

**Solution:**
1. Verify webhook URL is correct
2. Check webhook still exists in Discord server settings
3. For forum channels, ensure `?thread_id=xxx` is included

### "Webhook test failed (HTTP 404)"

**Cause:** Webhook URL doesn't exist or was deleted

**Solution:** Recreate webhook in Discord server settings

### "curl: (6) Could not resolve host"

**Cause:** Network connectivity issue or invalid URL format

**Solution:**
1. Check internet connection
2. Verify URL format: `https://discord.com/api/webhooks/ID/TOKEN`
3. No extra spaces or line breaks in URL

### Message doesn't appear in Discord

**Possible causes:**
1. Wrong webhook URL (sent to different server/channel)
2. Webhook deleted or disabled
3. Bot doesn't have permission to post in channel
4. Rate limiting (too many messages too quickly)

**Solutions:**
1. Verify webhook URL matches target Discord channel
2. Check webhook exists in Discord server settings
3. Test webhook URL manually with curl
4. Wait a few seconds and retry

## Creating Discord Webhooks

### For Text Channels

1. Open Discord Server Settings → Integrations → Webhooks
2. Click "New Webhook"
3. Name: "Auto Trader Alerts" (or similar)
4. Channel: Select target channel
5. Copy Webhook URL
6. Save

### For Forum Threads

1. Create webhook in forum channel (as above)
2. Append `?thread_id=THREAD_ID` to webhook URL
3. Find thread ID in Discord URL or Developer Tools

Example:
```
https://discord.com/api/webhooks/1234567890/AbCdEfGhIjKlMnOpQrStUvWxYz?thread_id=1479179730573066291
```

## Verification Checklist

Before marking subtask-6-3 as complete, verify:

- [ ] At least one Discord webhook is configured in `.env`
- [ ] Bash script test passes: `./scripts/test_discord_webhooks.sh`
- [ ] Test notifications appear in Discord channels
- [ ] Each notification has correct color (buy=green, sell=red, analysis=blue)
- [ ] All fields are populated correctly
- [ ] Embed formatting is clean and readable
- [ ] No errors in application logs

## Next Steps

Once E2E verification passes:

1. Mark subtask-6-3 as completed in `implementation_plan.json`
2. Update `build-progress.txt` with verification results
3. Commit changes with descriptive message
4. Proceed to final QA sign-off

## Support

For issues:
- Check logs: `tail -f logs/app.log`
- Test webhook URL manually with curl
- Verify Discord server permissions
- Check network connectivity
