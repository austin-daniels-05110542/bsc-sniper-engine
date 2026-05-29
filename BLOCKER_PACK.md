# Blocker Pack and Security Actions

## Blocker Pack (for Client)

### 1. Exact Endpoints Called
- **BscScan API**: `https://api-testnet.bscscan.com/api` (or mainnet variant)
  - Used for: `module=contract&action=getsourcecode&address={token_address}&apikey={key}`
- **BSC Node RPC**: URLs from config (e.g., `https://bsc-testnet.rpc.sentio.xyz`)
  - Used for: `eth_getLogs`, `eth_call`, `eth_getBlockByNumber`, etc.

### 2. Raw Error JSON Responses
- BscScan API error:
  ```json
  {"status":"0","message":"NOTOK","result":"Invalid API Key"}
  ```
- RPC error:
  ```json
  {"code":-32000,"message":"exceeds block range limit"}
  ```
- Internal error (Python exception):
  ```json
  {"detail":"Could not connect to any RPC. errors=[...]" }
  ```

### 3. Expected Request Volume
- BscScan API: 1 request per new token detected (typically <10/minute).
- RPC: 1-2 requests per block scanned, plus 1-2 per safety simulation (buy/sell path).

### 4. Why Blocked Checks Cannot Be Done On-Chain/RPC
- **Source code verification**: Only available via BscScan API, not on-chain.
- **Mint function detection**: Requires source or bytecode analysis, not always feasible via RPC alone.
- **Honeypot simulation**: Buy/sell path can be simulated via RPC, but some anti-bot or blacklist logic is only visible in source or via off-chain analysis.
- **Router validation**: Can be partially checked on-chain, but full validation may require off-chain data.

## Security Actions
- Private key will be removed from config and loaded securely from environment or secret manager.
- All exposed keys will be rotated.

---

This file is generated to fulfill the client's requirement for a precise blocker pack and to document security actions taken.
