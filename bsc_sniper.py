import argparse
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from pair_history import append_record

from web3 import Web3

try:
    from web3.middleware import geth_poa_middleware
except ImportError:  # web3.py v7 renamed this middleware.
    from web3.middleware import ExtraDataToPOAMiddleware as geth_poa_middleware


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


PAIR_CREATED_TOPIC = Web3.keccak(
    text="PairCreated(address,address,address,uint256)"
).hex()


def to_checksum(address: str) -> str:
    if hasattr(Web3, "to_checksum_address"):
        return Web3.to_checksum_address(address)
    return Web3.toChecksumAddress(address)


def from_wei(w3: Web3, amount: int, unit: str):
    if hasattr(w3, "from_wei"):
        return w3.from_wei(amount, unit)
    return w3.fromWei(amount, unit)


@dataclass
class PairCreatedEvent:
    block_number: int
    tx_hash: str
    token0: str
    token1: str
    pair: str


class BSCTokenSniper:
    def __init__(self, config_path: str = "config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.w3 = self.connect_rpc()

        self.chain_id = int(self.config["chainId"])
        self.factory_address = to_checksum(self.config["factoryAddress"])
        self.router_address = to_checksum(self.config["routerAddress"])
        self.wbnb_address = to_checksum(self.config["wbnbAddress"])
        quote_tokens = self.config.get("quoteTokenAddresses") or [self.config["wbnbAddress"]]
        self.quote_token_addresses = {to_checksum(addr).lower() for addr in quote_tokens}

        self.wallet_private_key = self.config.get("walletPrivateKey", "")
        self.account = None
        if self.wallet_private_key and "YOUR_PRIVATE_KEY" not in self.wallet_private_key:
            self.account = self.w3.eth.account.from_key(self.wallet_private_key)

        self.scanned_pairs = set()
        self.last_scanned_block = self.config.get("startBlock")
        self._factory_contract = None
        self._router_contract = None

        logger.info("Connected to chain_id=%s", self.chain_id)
        logger.info("Latest block: %s", self.w3.eth.block_number)
        if self.account:
            balance = from_wei(self.w3, self.w3.eth.get_balance(self.account.address), "ether")
            logger.info("Wallet: %s | Balance: %s BNB", self.account.address, balance)
        else:
            logger.info("No private key loaded. Running detect-only mode.")
        logger.info("Quote tokens for snipe targets: %s", ", ".join(sorted(self.quote_token_addresses)))

    def connect_rpc(self) -> Web3:
        rpc_urls: List[str] = []
        preferred = self.config.get("bscNode") or self.config.get("rpcUrl")
        if preferred:
            rpc_urls.append(preferred)
        rpc_urls.extend(self.config.get("rpcUrls") or [])
        # Preserve order but drop duplicates (first wins).
        seen = set()
        rpc_urls = [u for u in rpc_urls if u and not (u in seen or seen.add(u))]
        if not rpc_urls:
            raise RuntimeError("No RPC URL configured. Set bscNode, rpcUrl, or rpcUrls in config.")
        connection_errors = []

        for rpc_url in rpc_urls:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)

            try:
                is_connected = w3.is_connected() if hasattr(w3, "is_connected") else w3.isConnected()
                if is_connected:
                    logger.info("RPC connected: %s", rpc_url)
                    self.active_rpc_url = rpc_url
                    return w3
            except Exception as exc:
                connection_errors.append(f"{rpc_url}: {exc}")

        raise RuntimeError(f"Could not connect to any RPC. errors={connection_errors}")

    @staticmethod
    def _topic_address(topic) -> str:
        if hasattr(topic, "hex"):
            topic_hex = topic.hex()
        else:
            topic_hex = topic if isinstance(topic, str) else topic.hex()
        return to_checksum("0x" + topic_hex.replace("0x", "")[-40:])

    @staticmethod
    def _to_hex(value) -> str:
        if hasattr(value, "hex"):
            return value.hex()
        if isinstance(value, str):
            return value[2:] if value.startswith("0x") else value
        return value.hex()

    def decode_pair_created_log(self, log: Dict) -> PairCreatedEvent:
        token0 = self._topic_address(log["topics"][1])
        token1 = self._topic_address(log["topics"][2])

        data_hex = self._to_hex(log["data"])
        pair = to_checksum("0x" + data_hex[:64][-40:])

        tx_hash = self._to_hex(log["transactionHash"])
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash

        return PairCreatedEvent(
            block_number=int(log["blockNumber"]),
            tx_hash=tx_hash,
            token0=token0,
            token1=token1,
            pair=pair,
        )

    def _fetch_pair_created_logs(self, from_block: int, to_block: int) -> List[Dict]:
        return self.w3.eth.get_logs(
            {
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": self.factory_address,
                "topics": [PAIR_CREATED_TOPIC],
            }
        )

    def get_new_liquidity_pairs(self) -> List[PairCreatedEvent]:
        latest_block = self.w3.eth.block_number
        confirmation_blocks = int(self.config.get("confirmationBlocks", 1))
        to_block = max(latest_block - confirmation_blocks, 0)

        if self.last_scanned_block is None:
            lookback_blocks = int(self.config.get("initialLookbackBlocks", 100))
            from_block = max(to_block - lookback_blocks, 0)
        else:
            overlap_blocks = int(self.config.get("overlapBlocks", 2))
            from_block = max(int(self.last_scanned_block) - overlap_blocks + 1, 0)

        if from_block > to_block:
            logger.info("Waiting for new confirmed blocks. latest=%s", latest_block)
            return []

        max_scan_blocks = int(self.config.get("maxScanBlocks", 30))
        blocks_behind = to_block - from_block
        if blocks_behind > max_scan_blocks:
            logger.info(
                "Catching up: %s blocks (%s to %s) in chunks of %s",
                blocks_behind + 1,
                from_block,
                to_block,
                max_scan_blocks,
            )

        all_logs: List[Dict] = []
        chunk_start = from_block
        while chunk_start <= to_block:
            chunk_end = min(chunk_start + max_scan_blocks - 1, to_block)
            if blocks_behind <= max_scan_blocks:
                logger.info("Scanning PairCreated logs from block %s to %s", chunk_start, chunk_end)
            try:
                all_logs.extend(self._fetch_pair_created_logs(chunk_start, chunk_end))
            except ValueError as exc:
                logger.warning(
                    "RPC log scan was rejected. Reduce maxScanBlocks or use a private RPC. error=%s",
                    exc,
                )
                # Do not advance cursor past the failed chunk.
                if chunk_start == from_block:
                    return []
                self.last_scanned_block = chunk_start - 1
                break
            chunk_start = chunk_end + 1
        else:
            self.last_scanned_block = to_block

        new_pairs = []
        for raw_log in all_logs:
            event = self.decode_pair_created_log(raw_log)
            dedupe_key = f"{event.tx_hash}:{event.pair}"
            if dedupe_key in self.scanned_pairs:
                continue

            self.scanned_pairs.add(dedupe_key)
            new_pairs.append(event)

        if new_pairs:
            logger.info("Found %s new PairCreated event(s) in this cycle.", len(new_pairs))
        return new_pairs

    def get_factory_contract(self):
        if self._factory_contract is None:
            abi_path = Path(__file__).resolve().parent / "factory_abi.json"
            with open(abi_path, "r", encoding="utf-8") as f:
                factory_abi = json.load(f)
            self._factory_contract = self.w3.eth.contract(
                address=self.factory_address, abi=factory_abi
            )
        return self._factory_contract

    def get_router_contract(self):
        if self._router_contract is None:
            router_abi = json.loads(
                '[{"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},'
                '{"internalType":"address[]","name":"path","type":"address[]"}],'
                '"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts",'
                '"type":"uint256[]"}],"stateMutability":"view","type":"function"}]'
            )
            self._router_contract = self.w3.eth.contract(
                address=self.router_address, abi=router_abi
            )
        return self._router_contract

    def _build_and_send(self, tx: Dict[str, Any]) -> str:
        if not self.account:
            raise RuntimeError("walletPrivateKey is required")

        tx.setdefault("from", self.account.address)
        tx.setdefault("nonce", self.w3.eth.get_transaction_count(self.account.address))
        tx.setdefault("chainId", self.chain_id)

        if tx.get("type") == 2 or "maxFeePerGas" in tx:
            tx.pop("gasPrice", None)
        else:
            tx.setdefault("gasPrice", self.w3.eth.gas_price)

        if not tx.get("gas"):
            try:
                estimated = self.w3.eth.estimate_gas(tx)
                tx["gas"] = int(estimated * 1.2) + 50_000
            except Exception:
                tx["gas"] = int(self.config.get("gasLimit", 2_500_000))

        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        if not tx_hex.startswith("0x"):
            tx_hex = "0x" + tx_hex
        if receipt.status != 1:
            raise RuntimeError(f"Transaction reverted: {tx_hex}")
        return tx_hex

    def scan_block_range(
        self, from_block: int, to_block: int, chunk_blocks: Optional[int] = None
    ) -> List[PairCreatedEvent]:
        """Scan an explicit block range (used by API / UI test scan)."""
        if from_block > to_block:
            return []

        max_scan_blocks = chunk_blocks or int(self.config.get("manualScanChunkBlocks", 200))
        all_logs: List[Dict] = []
        chunk_start = from_block
        while chunk_start <= to_block:
            chunk_end = min(chunk_start + max_scan_blocks - 1, to_block)
            all_logs.extend(self._fetch_pair_created_logs(chunk_start, chunk_end))
            chunk_start = chunk_end + 1

        events = []
        seen = set()
        for raw_log in all_logs:
            event = self.decode_pair_created_log(raw_log)
            key = f"{event.tx_hash}:{event.pair}"
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
        return events

    def event_to_record(self, event: PairCreatedEvent, source: str = "scanner") -> Dict[str, Any]:
        target = self.choose_target_token(event)
        safety = self.analyze_token_safety(target) if target else None
        return {
            "block_number": event.block_number,
            "tx_hash": event.tx_hash,
            "pair": event.pair,
            "token0": event.token0,
            "token1": event.token1,
            "target_token": target,
            "snipeable": target is not None,
            "source": source,
            "safety": safety,
            "explorer_tx": self._explorer_url("tx", event.tx_hash),
            "explorer_pair": self._explorer_url("address", event.pair),
        }

    def _explorer_url(self, kind: str, value: str) -> str:
        base = self.config.get("explorerBaseUrl")
        if not base:
            if self.chain_id == 97:
                base = "https://testnet.bscscan.com"
            elif self.chain_id == 56:
                base = "https://bscscan.com"
            else:
                base = "https://testnet.bscscan.com"
        path = "tx" if kind == "tx" else "address"
        return f"{base}/{path}/{value}"

    def record_events(self, events: List[PairCreatedEvent], source: str = "scanner") -> List[Dict[str, Any]]:
        records = []
        for event in events:
            dedupe_key = f"{event.tx_hash}:{event.pair}"
            self.scanned_pairs.add(dedupe_key)
            record = self.event_to_record(event, source=source)
            append_record(record)
            records.append(record)
        return records

    def create_pair(self, token_a: str, token_b: str) -> Dict[str, Any]:
        """Call PancakeSwap factory createPair on testnet (emits PairCreated)."""
        if not self.account:
            raise RuntimeError("walletPrivateKey is required to create pairs on-chain")

        token_a = to_checksum(token_a)
        token_b = to_checksum(token_b)
        if token_a.lower() == token_b.lower():
            raise ValueError("tokenA and tokenB must be different addresses")

        factory = self.get_factory_contract()
        existing = factory.functions.getPair(token_a, token_b).call()
        zero = "0x0000000000000000000000000000000000000000"
        if existing and str(existing).lower() != zero:
            raise ValueError(
                f"Pair already exists at {to_checksum(existing)}. "
                "Use 'Deploy new token + create pair' instead."
            )

        tx = factory.functions.createPair(token_a, token_b).build_transaction({})
        tx_hex = self._build_and_send(tx)
        receipt = self.w3.eth.get_transaction_receipt(tx_hex)

        expected_topic = PAIR_CREATED_TOPIC.lower()
        if not expected_topic.startswith("0x"):
            expected_topic = "0x" + expected_topic

        detected = []
        for log in receipt.logs:
            if log["address"].lower() != self.factory_address.lower():
                continue
            topic0 = self._to_hex(log["topics"][0])
            if not topic0.startswith("0x"):
                topic0 = "0x" + topic0
            if topic0.lower() != expected_topic:
                continue
            raw_log = {
                "blockNumber": receipt.blockNumber,
                "transactionHash": tx_hex,
                "topics": log["topics"],
                "data": log["data"],
            }
            detected.append(self.decode_pair_created_log(raw_log))

        records = self.record_events(detected, source="create_pair")
        return {
            "tx_hash": tx_hex,
            "block_number": receipt.blockNumber,
            "events": records,
            "explorer_tx": self._explorer_url("tx", tx_hex),
        }

    def create_pair_with_new_token(self, quote_token: Optional[str] = None) -> Dict[str, Any]:
        """Deploy a fresh test ERC20, then createPair(quote, newToken)."""
        from test_token_deploy import deploy_test_token

        if not self.account:
            raise RuntimeError("walletPrivateKey is required to create pairs on-chain")

        quote = to_checksum(quote_token or self.wbnb_address)
        suffix = int(time.time()) % 1_000_000
        token_name = f"SnipeTest{suffix}"
        token_symbol = f"S{suffix % 10000}"

        new_token, deploy_info = deploy_test_token(
            self.w3,
            self.account,
            self.chain_id,
            name=token_name,
            symbol=token_symbol,
        )
        logger.info("Deployed test token %s (%s)", new_token, token_symbol)

        pair_result = self.create_pair(quote, new_token)
        pair_result["deployed_token"] = new_token
        pair_result["deploy"] = deploy_info
        return pair_result

    def choose_target_token(self, event: PairCreatedEvent) -> Optional[str]:
        token0 = event.token0.lower()
        token1 = event.token1.lower()
        token0_is_quote = token0 in self.quote_token_addresses
        token1_is_quote = token1 in self.quote_token_addresses

        if token0_is_quote and not token1_is_quote:
            return event.token1
        if token1_is_quote and not token0_is_quote:
            return event.token0
        return None

    def analyze_token_safety(self, token_address: str) -> Dict:
        token_address = to_checksum(token_address)
        report: Dict[str, Any] = {
            "is_safe": False,
            "checks_passed": [],
            "checks_failed": [],
            "risk_score": 100,
        }

        if self.config.get("disableSafetyForTesting", False):
            report["is_safe"] = True
            report["bypassed"] = True
            report["checks_passed"].append("testnet_safety_disabled")
            return report

        if not self.config.get("enableMiniAudit", True):
            report["is_safe"] = True
            report["checks_passed"].append("mini_audit_disabled")
            return report

        code = self.w3.eth.get_code(token_address)
        if not code or code in (b"", b"0x", "0x"):
            report["checks_failed"].append("no_contract_code")
            report["risk_score"] -= 100
            return report
        report["checks_passed"].append("contract_exists")

        router = self.get_router_contract()
        probe_wei = self.w3.to_wei(self.config.get("safetyProbeBnb", 0.001), "ether")

        if self.config.get("checkHoneypot", True):
            buy_path = [self.wbnb_address, token_address]
            sell_path = [token_address, self.wbnb_address]
            buy_out = None
            try:
                buy_out = router.functions.getAmountsOut(probe_wei, buy_path).call()
                if buy_out[-1] > 0:
                    report["checks_passed"].append("buy_path_ok")
                else:
                    report["checks_failed"].append("buy_path_zero")
                    report["risk_score"] -= 40
            except Exception:
                report["checks_failed"].append("buy_path_reverted")
                report["risk_score"] -= 40

            if buy_out and buy_out[-1] > 0:
                try:
                    token_probe = max(buy_out[-1] // 100, 1)
                    sell_out = router.functions.getAmountsOut(token_probe, sell_path).call()
                    if sell_out[-1] > 0:
                        report["checks_passed"].append("sell_path_ok")
                    else:
                        report["checks_failed"].append("sell_path_zero")
                        report["risk_score"] -= 50
                except Exception:
                    report["checks_failed"].append("sell_path_reverted")
                    report["risk_score"] -= 50

        if self.config.get("checkSourceCode", False) and self.config.get("bscScanAPIKey"):
            import requests

            api_url = self.config.get("bscScanApiUrl", "https://api-testnet.bscscan.com/api")
            try:
                response = requests.get(
                    api_url,
                    params={
                        "module": "contract",
                        "action": "getsourcecode",
                        "address": token_address,
                        "apikey": self.config["bscScanAPIKey"],
                    },
                    timeout=15,
                )
                data = response.json()
                if data.get("status") == "1" and data.get("result", [{}])[0].get("SourceCode"):
                    report["checks_passed"].append("source_verified")
                else:
                    report["checks_failed"].append("source_unverified")
                    report["risk_score"] -= 20
            except Exception:
                report["checks_failed"].append("source_check_error")

        if not report["checks_failed"]:
            report["is_safe"] = True
        elif report["risk_score"] >= int(self.config.get("minSafetyScore", 50)):
            report["is_safe"] = bool(self.config.get("allowMediumRisk", False))

        if self.config.get("disableSafetyForTesting", False):
            report["is_safe"] = True
            report["bypassed"] = True
            report["checks_passed"].append("testnet_bypass")

        return report

    def buy_token(self, token_address: str) -> Optional[str]:
        if self.config.get("dryRun", True):
            logger.info("[DRY RUN] Would buy token: %s", token_address)
            return None

        if not self.account:
            raise RuntimeError("walletPrivateKey is required when dryRun=false")

        # Real buy execution is intentionally added after detection and safety checks are proven.
        raise NotImplementedError("Buy execution will be implemented in the next step.")

    def run_once(self) -> List[Dict[str, Any]]:
        new_pairs = self.get_new_liquidity_pairs()
        if not new_pairs:
            logger.info("No new PairCreated events in this cycle.")
            return []

        records = self.record_events(new_pairs, source="scanner")
        snipeable = 0
        for event in new_pairs:
            target_token = self.choose_target_token(event)
            logger.info(
                "NEW PAIR | pair=%s | token0=%s | token1=%s | block=%s | tx=%s",
                event.pair,
                event.token0,
                event.token1,
                event.block_number,
                event.tx_hash,
            )

            if not target_token:
                logger.info(
                    "Not snipeable: neither side matches quote tokens %s",
                    sorted(self.quote_token_addresses),
                )
                continue

            snipeable += 1
            logger.info("SNIPE TARGET | token=%s | pair=%s", target_token, event.pair)
            safety = self.analyze_token_safety(target_token)
            if safety["is_safe"]:
                self.buy_token(target_token)
            else:
                logger.info("Skipping buy. Failed checks: %s", safety["checks_failed"])

        logger.info(
            "Cycle summary: %s PairCreated event(s), %s snipeable quote-side pair(s)",
            len(new_pairs),
            snipeable,
        )
        return records

    def get_status(self) -> Dict[str, Any]:
        balance = None
        if self.account:
            balance = str(from_wei(self.w3, self.w3.eth.get_balance(self.account.address), "ether"))
        return {
            "network": self.config.get("networkName"),
            "chain_id": self.chain_id,
            "rpc_url": getattr(self, "active_rpc_url", None),
            "latest_block": self.w3.eth.block_number,
            "last_scanned_block": self.last_scanned_block,
            "factory": self.factory_address,
            "wallet": self.account.address if self.account else None,
            "balance_bnb": balance,
            "quote_tokens": sorted(self.quote_token_addresses),
            "dry_run": self.config.get("dryRun", True),
            "safety_mode": (
                "bypassed (testnet)"
                if self.config.get("disableSafetyForTesting")
                else "mini-audit on"
            ),
        }

    def run(self) -> None:
        poll_seconds = int(self.config.get("pollSeconds", 10))
        logger.info("BSC sniper detection loop started. pollSeconds=%s", poll_seconds)
        while True:
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Cycle failed: %s", exc)
            time.sleep(poll_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BSC sniper bot")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    parser.add_argument("--once", action="store_true", help="Run one detection cycle and exit")
    args = parser.parse_args()

    bot = BSCTokenSniper(args.config)
    if args.once:
        bot.run_once()
    else:
        bot.run()
