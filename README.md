# Memecoin Paper Bot — 20 EUR

Separate Solana paper-trading service. No wallet, signing or live trades.
Each buy debits exactly 20 EUR including modelled buy costs. Initial virtual cash: 200 EUR.
Maximum 3 open positions, 5 buys per UTC day, 24-hour token cooldown.
New buys halt after 20 EUR marked daily loss. This is not a guaranteed maximum loss.
Net exit triggers: <=17 EUR stop, >=26 EUR target, or 4 hours holding time.

Automatic discovery uses up to 24 latest Solana token profiles whose own description mentions
meme/memecoin/memetoken. This is not independent classification or a complete market scan.
Selected pools require at least 100,000 USD liquidity, 25,000 USD hourly volume,
age 1 hour–30 days, recent buys and sells, positive capped momentum confirmed in two observations.
Only legacy SPL mints with revoked mint/freeze authority pass. These checks cannot prove safety.

Paper costs per side: 0.5% fee, 0.10 EUR fixed cost and 1% adverse slippage.
Prices are DEX Screener indications, not executable swap quotes. Token quantity is theoretical.
FX is a dated ECB reference rate, max age 5 days, not an executable exchange rate.
Missing data blocks new buys and leaves unpriceable positions open. No fictitious liquidation.
The thresholds are unvalidated design assumptions, not a demonstrated profitable strategy.

## Running and commands

Python 3.12+, standard library only.
Run: python launch.py run
Status: python bot.py status
Pause buys: python bot.py pause
Resume buys: python bot.py resume
Request all exits and pause: python bot.py close-all
Export: python bot.py export --output trades.csv
The run process must remain running to monitor and close positions.
Commands must use the same DATA_DIR. Pause/resume cannot override the daily halt.
An incomplete day-start valuation blocks entries for that UTC day.

Railway requires exactly one instance with its own volume at /data.
launch.py refuses Railway startup without the persistent mount and logs source probes plus heartbeats.
SQLite stores positions, balance, day limits and events. Keep the whole data directory when backing up.

Optional variables: DISCORD_WEBHOOK_URL, SOLANA_RPC_URL, WATCH_MINTS.
DATA_DIR=/data and TRADING_MODE=paper are fixed deployment settings.
No Discord slash commands: optional webhook sends trade events. It needs a separately configured
channel webhook; no existing stock-bot token is copied. Missing notifications do not stop monitoring.
Webhook delivery can duplicate an event after an interrupted response; event IDs remain stable.

## Validation

Docker build runs the 27 accounting/scanner/integration tests plus 3 volume-guard tests.
Build must fail if any test fails. Source connectivity is checked read-only at runtime.
Offline tests do not establish profitability or successful blockchain execution.

## Sources

- [DEX Screener API](https://docs.dexscreener.com/api/reference)
- [Solana RPC](https://solana.com/docs/rpc/http/getaccountinfo)
- [Solana authorities](https://solana.com/docs/tokens/basics/set-authority)
- [ECB reference rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)
- [Discord webhooks](https://docs.discord.com/developers/resources/webhook)

## Rate limits

HTTP 429 obeys Retry-After (seconds or HTTP date). Without that header the client waits
60 seconds, then doubles up to 960 seconds between unsuccessful attempts. The wait applies
per host; it does not block unrelated data providers or the process heartbeat. Missing data
continues to block buys. The 35 offline tests also run on process startup for visible verification.
