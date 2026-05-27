# BSC Sniper Bot Testnet Starter

This first version only proves the detection layer.

It connects directly to BSC Testnet RPC and scans PancakeSwap `PairCreated` logs from the factory contract. It does not buy tokens yet while `dryRun` is `true`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.json config.json
python bsc_sniper.py --once
```

## What To Show The Client

The useful proof is console logs showing:

- RPC connected
- latest block number
- scan range
- detected `PairCreated` events, if any
- dry-run buy decision for WBNB pairs

For continuous scanning:

```powershell
python bsc_sniper.py
```

## Web dashboard (create pair · scan · history)

```powershell
pip install -r requirements.txt
python api_server.py
```

Open http://localhost:8765 in your browser.

- **Create pair** — sends a testnet `createPair` tx (needs `walletPrivateKey` in config)
- **Test scan** — fetches `PairCreated` logs for a block range
- **History** — all detected / created events saved to `data/pair_history.json`

## Important

Use only a testnet private key. Never put a real wallet private key in this file during development.
