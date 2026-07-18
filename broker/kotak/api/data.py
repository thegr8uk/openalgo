import base64
import json
import time
import urllib.parse
from datetime import datetime

import httpx
import pandas as pd

from broker.kotak.database.master_contract_db import SymToken, db_session
from database.token_db import get_br_symbol, get_brexchange, get_token
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


class BrokerData:
    def __init__(self, auth_token):
        # Updated for Neo API v2: session_token:::session_sid:::base_url:::access_token
        self.session_token, self.session_sid, self.base_url, self.access_token = auth_token.split(
            ":::"
        )

        # baseUrl is mandatory; it comes from MPIN validation. Raise if missing.
        if not self.base_url or not self.base_url.startswith("http"):
            raise ValueError(
                "Kotak auth token missing baseUrl. Please re-login (TOTP + MPIN) to refresh credentials."
            )

        self.base_url = self.base_url.rstrip("/")
        self.quotes_base_url = self.base_url  # Use broker-provided baseUrl for quotes
        self.last_quote_error = None
        logger.info(f"Using quotes baseUrl: {self.quotes_base_url}")

        # CMOTS TradingView API interval mapping
        # Supported: 1,2,3,5,10,15,30,60,75,120,125,180,D,W,M
        self.timeframe_map = {
            "1m":  "1",
            "2m":  "2",
            "3m":  "3",
            "5m":  "5",
            "10m": "10",
            "15m": "15",
            "30m": "30",
            "60m": "60",
            "1h":  "60",
            "75m": "75",
            "120m": "120",
            "125m": "125",
            "180m": "180",
            "D":   "D",
            "W":   "W",
            "M":   "M",
        }

        # CMOTS history base URL (no auth required - uses public token endpoint)
        self.cmots_base_url = "https://nksapi.kotaksecurities.com/1newserviceapi/cmots/v1/equity/TradingViewData_AllAssetNew/i/"

    def _get_kotak_exchange(self, exchange):
        """Map OpenAlgo exchange to Kotak exchange segment"""
        exchange_map = {
            "NSE": "nse_cm",
            "BSE": "bse_cm",
            "NFO": "nse_fo",
            "BFO": "bse_fo",
            "CDS": "cde_fo",
            "MCX": "mcx_fo",
            "NSE_INDEX": "nse_cm",
            "BSE_INDEX": "bse_cm",
        }
        return exchange_map.get(exchange)

    def _get_index_symbol_candidates(self, symbol):
        """Return candidate Neo API neoSymbol names for an OpenAlgo index symbol.

        Kotak Neo's /quotes/neosymbol endpoint expects an exact name match. The
        canonical name differs per index and is not always derivable from the
        master contract (which often stores just the short ticker). We try
        descriptive variants in priority order and stop at the first hit.
        """
        index_map = {
            "NIFTY": ["Nifty 50"],
            "NIFTY50": ["Nifty 50"],
            "BANKNIFTY": ["Nifty Bank"],
            "FINNIFTY": ["Nifty Fin Service"],
            "MIDCPNIFTY": [
                "Nifty Mid Select",
                "Nifty Midcap Sel",
                "Nifty Midcap Select",
                "NIFTY MID SELECT",
            ],
            "NIFTYNXT50": ["Nifty Next 50"],
            "INDIAVIX": ["India VIX"],
            "SENSEX": ["SENSEX"],
            "BANKEX": ["BANKEX"],
        }
        key = symbol.upper()
        return index_map.get(key, [symbol])

    def _make_quotes_request(self, query, filter_name="all"):
        """Make HTTP request to Neo API v2 quotes endpoint using httpx connection pooling"""
        client = get_httpx_client()

        # URL encode spaces but keep pipe/comma characters
        encoded_query = urllib.parse.quote(query, safe="|,")
        endpoint = f"/script-details/1.0/quotes/neosymbol/{encoded_query}/{filter_name}"

        headers = {"Authorization": self.access_token, "Content-Type": "application/json"}

        url = f"{self.quotes_base_url}{endpoint}"
        last_error = None

        try:
            logger.info(f"QUOTES API - Making request to: {url}")
            logger.debug(f"QUOTES API - Using access_token: {self.access_token[:10]}...")

            response = client.get(url, headers=headers)
            logger.info(f"QUOTES API - Response status: {response.status_code} for {url}")

            if response.status_code == 200:
                response_data = json.loads(response.text)
                logger.debug(
                    f"QUOTES API - Raw response: {response.text[:200]}..."
                )  # Log first 200 chars

                # Kotak Neo returns 200 with {"stat":"Not_Ok","emsg":...,"stCode":1009}
                # when the instrument/code is invalid. Surface that as an error.
                if isinstance(response_data, dict) and response_data.get("stat") == "Not_Ok":
                    self.last_quote_error = {
                        "stat": "Not_Ok",
                        "emsg": response_data.get("emsg"),
                        "stCode": response_data.get("stCode"),
                        "url": url,
                    }
                    logger.warning(
                        f"QUOTES API - Neo error: {response_data.get('emsg')} (stCode={response_data.get('stCode')})"
                    )
                    return None

                # Log the complete structure for debugging (only for depth requests)
                if (
                    "depth" in endpoint
                    and response_data
                    and isinstance(response_data, list)
                    and len(response_data) > 0
                ):
                    logger.debug(
                        f"DEPTH API - Complete raw response structure: {json.dumps(response_data[0], indent=2)}"
                    )

                self.last_quote_error = None
                return response_data

            last_error = {"status": response.status_code, "body": response.text[:500], "url": url}
            logger.warning(f"QUOTES API - HTTP {response.status_code}: {response.text[:200]}...")

        except httpx.HTTPError as e:
            last_error = {"error": str(e), "url": url}
            logger.error(f"HTTP error in _make_quotes_request ({url}): {e}")
        except Exception as e:
            last_error = {"error": str(e), "url": url}
            logger.error(f"Error in _make_quotes_request ({url}): {e}")

        self.last_quote_error = last_error
        return None

    def _query_index_with_candidates(self, kotak_exchange, candidates, filter_name="all"):
        """Try each candidate index name until one returns data.

        Kotak Neo's neoSymbol endpoint requires exact case-sensitive names that
        aren't always present in the scrip master, so we probe known variants.
        Returns (response, query_used) or (None, last_query_tried).
        """
        last_query = None
        for cand in candidates:
            query = f"{kotak_exchange}|{cand}"
            last_query = query
            response = self._make_quotes_request(query, filter_name)
            if response and isinstance(response, list) and len(response) > 0:
                return response, query
        return None, last_query

    def get_quotes(self, symbol, exchange):
        """Get live quotes using Neo API v2 quotes endpoint with pSymbol-based queries"""
        try:
            logger.info(f"QUOTES API - Symbol: {symbol}, Exchange: {exchange}")

            # Check if this is an index - use symbol name instead of pSymbol
            if "INDEX" in exchange.upper():
                # For indices, map to correct Neo API format and use static exchange mapping
                kotak_exchange = self._get_kotak_exchange(exchange)
                candidates = self._get_index_symbol_candidates(symbol)
                logger.info(
                    f"QUOTES API - Index candidates for {symbol}: {candidates}"
                )
                response, query = self._query_index_with_candidates(
                    kotak_exchange, candidates, "all"
                )
                if response is None:
                    logger.error(
                        f"QUOTES API - All index candidates failed for {symbol}; last query: {query}"
                    )
                    return None
                logger.info(f"QUOTES API - Index resolved via: {query}")
            else:
                # For regular stocks/F&O, get both pSymbol and brexchange from database
                # In Kotak DB: token = pSymbol, brexchange = nse_cm/nse_fo/bse_cm etc.
                psymbol = get_token(symbol, exchange)
                brexchange = get_brexchange(symbol, exchange)
                logger.info(f"QUOTES API - pSymbol: {psymbol}, brexchange: {brexchange}")

                if not psymbol or not brexchange:
                    logger.error(f"pSymbol or brexchange not found for {symbol} on {exchange}")
                    return self._get_default_quote()

                # Map brexchange to correct Kotak format if needed
                if brexchange in ["NSE", "BSE", "NFO", "BFO", "CDS", "MCX"]:
                    kotak_exchange = self._get_kotak_exchange(brexchange)
                    logger.info(f"QUOTES API - Mapped {brexchange} to {kotak_exchange}")
                else:
                    kotak_exchange = brexchange  # Already in correct format

                # Build query using mapped exchange: kotak_exchange|pSymbol
                query = f"{kotak_exchange}|{psymbol}"
                logger.info(f"QUOTES API - Query: {query}")

                # Make API request (index branch already fetched response above)
                response = self._make_quotes_request(query, "all")

            if response and isinstance(response, list) and len(response) > 0:
                quote_data = response[0]
                logger.info(
                    f"QUOTES API - Query successful for: {quote_data.get('display_symbol')}"
                )
            else:
                logger.error(
                    f"QUOTES API - Query failed for {symbol}; last_error={self.last_quote_error}"
                )
                return None

            if response and isinstance(response, list) and len(response) > 0:
                quote_data = response[0]

                # Parse Neo API v2 response format (based on actual API response)
                ohlc_data = quote_data.get("ohlc", {})
                ltp_parsed = float(quote_data.get("ltp", 0))

                # Get depth data for actual bid/ask prices
                depth_data = quote_data.get("depth", {})
                buy_orders = depth_data.get("buy", [])
                sell_orders = depth_data.get("sell", [])

                # Extract best bid and ask prices from depth
                bid_price = float(buy_orders[0].get("price", 0)) if buy_orders else ltp_parsed
                ask_price = float(sell_orders[0].get("price", 0)) if sell_orders else ltp_parsed

                # Get total quantities (for reference)
                total_buy_qty = quote_data.get("total_buy", 0)
                total_sell_qty = quote_data.get("total_sell", 0)

                logger.debug(
                    f"QUOTES API - Parsing for {quote_data.get('display_symbol', 'unknown')}:"
                )
                logger.debug(f"  - ltp: {ltp_parsed}")
                logger.debug(f"  - total_buy_qty: {total_buy_qty} (quantity, not price)")
                logger.debug(f"  - total_sell_qty: {total_sell_qty} (quantity, not price)")
                logger.debug(f"  - best_bid_price: {bid_price}")
                logger.debug(f"  - best_ask_price: {ask_price}")

                return {
                    "bid": bid_price,
                    "ask": ask_price,
                    "open": float(ohlc_data.get("open", 0)),
                    "high": float(ohlc_data.get("high", 0)),
                    "low": float(ohlc_data.get("low", 0)),
                    "ltp": ltp_parsed,
                    "prev_close": float(ohlc_data.get("close", 0)),
                    "volume": float(quote_data.get("last_volume", 0)),
                    "oi": int(quote_data.get("open_int", 0)),  # Available in response
                }
            elif response is not None:
                # API returned 200 but empty response - this is normal for some symbols
                logger.info(f"Empty response received for {symbol} - API returned 200 but no data")
                return self._get_default_quote()
            else:
                logger.warning(f"No quote data received for {symbol}")
                return self._get_default_quote()

        except Exception as e:
            logger.error(f"Error in get_quotes: {e}")
            return self._get_default_quote()

    def get_depth(self, symbol: str, exchange: str) -> dict:
        """Get market depth using Neo API v2 quotes endpoint with depth filter"""
        try:
            logger.info(f"DEPTH API - Symbol: {symbol}, Exchange: {exchange}")

            # Check if this is an index - use symbol name instead of pSymbol
            if "INDEX" in exchange.upper():
                # For indices, map to correct Neo API format and use static exchange mapping
                kotak_exchange = self._get_kotak_exchange(exchange)
                candidates = self._get_index_symbol_candidates(symbol)
                logger.debug(
                    f"DEPTH API - Index candidates for {symbol}: {candidates}"
                )
                response, query = self._query_index_with_candidates(
                    kotak_exchange, candidates, "depth"
                )
                if response is None:
                    logger.warning(
                        f"DEPTH API - All index candidates failed for {symbol}; last query: {query}"
                    )
                    return self._get_default_depth()
                logger.debug(f"DEPTH API - Index resolved via: {query}")
            else:
                # For regular stocks/F&O, get both pSymbol and brexchange from database
                # In Kotak DB: token = pSymbol, brexchange = nse_cm/nse_fo/bse_cm etc.
                psymbol = get_token(symbol, exchange)
                brexchange = get_brexchange(symbol, exchange)
                logger.info(f"DEPTH API - pSymbol: {psymbol}, brexchange: {brexchange}")

                if not psymbol or brexchange is None:
                    logger.error(f"pSymbol or brexchange not found for {symbol} on {exchange}")
                    return self._get_default_depth()

                # Map brexchange to correct Kotak format if needed
                if brexchange in ["NSE", "BSE", "NFO", "BFO", "CDS", "MCX"]:
                    kotak_exchange = self._get_kotak_exchange(brexchange)
                    logger.info(f"DEPTH API - Mapped {brexchange} to {kotak_exchange}")
                else:
                    kotak_exchange = brexchange  # Already in correct format

                # Build query using mapped exchange: kotak_exchange|pSymbol
                query = f"{kotak_exchange}|{psymbol}"
                logger.debug(f"DEPTH API - Query: {query}")

                # Make API request with depth filter (index branch already fetched response)
                response = self._make_quotes_request(query, "depth")

            if response and isinstance(response, list) and len(response) > 0:
                target_quote = response[0]
                depth_data = target_quote.get("depth", {})

                logger.debug(f"DEPTH API - Raw depth data: {depth_data}")

                # Parse Neo API v2 depth format (based on actual API response)
                bids = []
                asks = []

                # Process buy orders (bids) - handle both array and object formats
                buy_data = depth_data.get("buy", [])
                logger.debug(f"DEPTH API - Buy data: {buy_data}")

                if isinstance(buy_data, list):
                    for i, bid in enumerate(buy_data[:5]):  # Top 5 bids
                        logger.debug(f"DEPTH API - Processing bid {i}: {bid}")
                        bids.append(
                            {
                                "price": float(bid.get("price", 0)),
                                "quantity": int(bid.get("quantity", 0)),
                            }
                        )

                # Process sell orders (asks) - handle both array and object formats
                sell_data = depth_data.get("sell", [])
                logger.debug(f"DEPTH API - Sell data: {sell_data}")

                if isinstance(sell_data, list):
                    for i, ask in enumerate(sell_data[:5]):  # Top 5 asks
                        logger.debug(f"DEPTH API - Processing ask {i}: {ask}")
                        asks.append(
                            {
                                "price": float(ask.get("price", 0)),
                                "quantity": int(ask.get("quantity", 0)),
                            }
                        )

                logger.debug(f"DEPTH API - Parsed bids: {bids}")
                logger.debug(f"DEPTH API - Parsed asks: {asks}")

                # Ensure we have 5 levels
                while len(bids) < 5:
                    bids.append({"price": 0, "quantity": 0})
                while len(asks) < 5:
                    asks.append({"price": 0, "quantity": 0})

                total_buy_qty = sum(bid["quantity"] for bid in bids if bid["quantity"] > 0)
                total_sell_qty = sum(ask["quantity"] for ask in asks if ask["quantity"] > 0)

                result = {
                    "bids": bids,
                    "asks": asks,
                    "totalbuyqty": total_buy_qty,
                    "totalsellqty": total_sell_qty,
                }

                logger.debug(f"DEPTH API - Final result: {result}")
                return result
            else:
                logger.warning(f"No depth data received for {symbol}")
                return self._get_default_depth()

        except Exception as e:
            logger.error(f"Error in get_depth: {e}")
            return self._get_default_depth()

    def get_multiquotes(self, symbols: list) -> list:
        """
        Get real-time quotes for multiple symbols with automatic batching
        Args:
            symbols: List of dicts with 'symbol' and 'exchange' keys
                     Example: [{'symbol': 'SBIN', 'exchange': 'NSE'}, ...]
        Returns:
            list: List of quote data for each symbol with format:
                  [{'symbol': 'SBIN', 'exchange': 'NSE', 'data': {...}}, ...]
        """
        try:
            BATCH_SIZE = 50  # Conservative limit for URL length (GET request)
            RATE_LIMIT_DELAY = 0.2  # 5 requests/sec = 250 symbols/sec (under 500 limit)

            # If symbols exceed batch size, process in batches
            if len(symbols) > BATCH_SIZE:
                logger.info(f"Processing {len(symbols)} symbols in batches of {BATCH_SIZE}")
                all_results = []

                # Split symbols into batches
                for i in range(0, len(symbols), BATCH_SIZE):
                    batch = symbols[i : i + BATCH_SIZE]
                    logger.debug(
                        f"Processing batch {i // BATCH_SIZE + 1}: symbols {i + 1} to {min(i + BATCH_SIZE, len(symbols))}"
                    )

                    # Process this batch
                    batch_results = self._process_quotes_batch(batch)
                    all_results.extend(batch_results)

                    # Rate limit delay between batches
                    if i + BATCH_SIZE < len(symbols):
                        time.sleep(RATE_LIMIT_DELAY)

                logger.info(
                    f"Successfully processed {len(all_results)} quotes in {(len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE} batches"
                )
                return all_results
            else:
                # Single batch processing
                return self._process_quotes_batch(symbols)

        except Exception as e:
            logger.exception("Error fetching multiquotes")
            raise Exception(f"Error fetching multiquotes: {e}")

    def _process_quotes_batch(self, symbols: list) -> list:
        """
        Process a single batch of symbols (internal method)
        Args:
            symbols: List of dicts with 'symbol' and 'exchange' keys (max 50)
        Returns:
            list: List of quote data for the batch
        """
        # Build comma-separated queries and mapping
        queries = []
        query_map = {}  # {query -> {symbol, exchange}}
        skipped_symbols = []  # Track symbols that couldn't be resolved

        for item in symbols:
            symbol = item["symbol"]
            exchange = item["exchange"]

            try:
                # Check if this is an index
                if "INDEX" in exchange.upper():
                    kotak_exchange = self._get_kotak_exchange(exchange)
                    # Batch path uses the first candidate; single-symbol path
                    # (get_quotes/get_depth) iterates all candidates.
                    candidates = self._get_index_symbol_candidates(symbol)
                    neo_symbol = candidates[0]
                    query = f"{kotak_exchange}|{neo_symbol}"
                else:
                    # For regular stocks/F&O, get pSymbol and brexchange
                    psymbol = get_token(symbol, exchange)
                    brexchange = get_brexchange(symbol, exchange)

                    if not psymbol or not brexchange:
                        logger.warning(
                            f"Skipping symbol {symbol} on {exchange}: could not resolve pSymbol or brexchange"
                        )
                        skipped_symbols.append(
                            {
                                "symbol": symbol,
                                "exchange": exchange,
                                "error": "Could not resolve pSymbol or brexchange",
                            }
                        )
                        continue

                    # Map brexchange to Kotak format if needed
                    if brexchange in ["NSE", "BSE", "NFO", "BFO", "CDS", "MCX"]:
                        kotak_exchange = self._get_kotak_exchange(brexchange)
                    else:
                        kotak_exchange = brexchange

                    query = f"{kotak_exchange}|{psymbol}"

                queries.append(query)
                query_map[query] = {"symbol": symbol, "exchange": exchange}

            except Exception as e:
                logger.warning(f"Skipping symbol {symbol} on {exchange}: {str(e)}")
                skipped_symbols.append({"symbol": symbol, "exchange": exchange, "error": str(e)})
                continue

        # Return skipped symbols if no valid queries
        if not queries:
            logger.warning("No valid queries to fetch quotes for")
            return skipped_symbols

        # Build comma-separated query string
        combined_query = ",".join(queries)

        logger.info(f"Requesting quotes for {len(queries)} instruments")
        logger.debug(
            f"Combined query: {combined_query[:200]}..."
            if len(combined_query) > 200
            else f"Combined query: {combined_query}"
        )

        # Make API request using existing method (handles URL encoding)
        response_data = self._make_quotes_request(combined_query, "all")
        if response_data is None:
            logger.error(f"API Error: {self.last_quote_error}")
            raise Exception(f"API Error: {self.last_quote_error}")

        # Parse response and build results
        results = []

        if not response_data or not isinstance(response_data, list):
            logger.warning("Empty or invalid response from API")
            return results

        # Build lookup by query for response matching
        # Response items have 'exchange' and 'exchange_token' or 'display_symbol'
        response_lookup = {}
        for quote in response_data:
            # Build possible keys to match
            exch = quote.get("exchange", "")
            token = quote.get("exchange_token", "")
            display = quote.get("display_symbol", "")

            # Try to match with original query format
            key1 = f"{exch}|{token}"
            key2 = f"{exch}|{display.replace('-EQ', '').replace('-IN', '')}" if display else None

            response_lookup[key1] = quote
            if key2:
                response_lookup[key2] = quote

        # Build results from query_map
        for query, original in query_map.items():
            # Try to find matching quote in response
            quote_data = response_lookup.get(query)

            # If not found, try variations
            if not quote_data:
                for resp_key, resp_quote in response_lookup.items():
                    if query.lower() == resp_key.lower():
                        quote_data = resp_quote
                        break

            if not quote_data:
                logger.warning(f"No quote data found for {original['symbol']} ({query})")
                results.append(
                    {
                        "symbol": original["symbol"],
                        "exchange": original["exchange"],
                        "error": "No quote data available",
                    }
                )
                continue

            # Parse and format quote data
            ohlc_data = quote_data.get("ohlc", {})
            depth_data = quote_data.get("depth") or {}  # Guard against null depth
            buy_orders = depth_data.get("buy", [])
            sell_orders = depth_data.get("sell", [])

            ltp = float(quote_data.get("ltp", 0))
            bid_price = float(buy_orders[0].get("price", 0)) if buy_orders else ltp
            ask_price = float(sell_orders[0].get("price", 0)) if sell_orders else ltp

            result_item = {
                "symbol": original["symbol"],
                "exchange": original["exchange"],
                "data": {
                    "bid": bid_price,
                    "ask": ask_price,
                    "open": float(ohlc_data.get("open", 0)),
                    "high": float(ohlc_data.get("high", 0)),
                    "low": float(ohlc_data.get("low", 0)),
                    "ltp": ltp,
                    "prev_close": float(ohlc_data.get("close", 0)),
                    "volume": float(quote_data.get("last_volume", 0)),
                    "oi": int(quote_data.get("open_int", 0)),
                },
            }
            results.append(result_item)

        # Include skipped symbols in results
        return skipped_symbols + results

    def _get_default_quote(self):
        """Return default quote structure"""
        return {
            "bid": 0,
            "ask": 0,
            "open": 0,
            "high": 0,
            "low": 0,
            "ltp": 0,
            "prev_close": 0,
            "volume": 0,
            "oi": 0,
        }

    def _get_default_depth(self):
        """Return default depth structure"""
        return {
            "bids": [{"price": 0, "quantity": 0} for _ in range(5)],
            "asks": [{"price": 0, "quantity": 0} for _ in range(5)],
            "totalbuyqty": 0,
            "totalsellqty": 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers for historical data
    # ------------------------------------------------------------------

    def _resolve_fno_details(self, symbol: str, exchange: str) -> dict | None:
        """
        Query the DB for an FNO/BFO instrument and return the fields needed
        to build the CMOTS payload.

        Returns a dict with keys:
          underlying, expiry_api (DD-MON-YYYY), strike_api, opttype, brexchange
        or None if the symbol cannot be resolved.
        """
        br_symbol = get_br_symbol(symbol, exchange)
        if not br_symbol:
            logger.error(f"Cannot resolve br_symbol for {symbol}/{exchange}")
            return None

        try:
            with db_session() as session:
                row = (
                    session.query(SymToken)
                    .filter(SymToken.exchange == exchange, SymToken.brsymbol == br_symbol)
                    .first()
                )
                if not row:
                    logger.error(f"No DB row for {exchange}:{br_symbol}")
                    return None

                # name stores the underlying root (e.g. NIFTY, BANKNIFTY, RELIANCE)
                underlying = row.name
                instrumenttype = row.instrumenttype or ""
                brexchange = row.brexchange or ""

                # expiry in DB: DD-MON-YY  → convert to DD-MON-YYYY for API
                expiry_db = row.expiry or "-"  # e.g. "12-MAY-26"
                if expiry_db and expiry_db != "-":
                    try:
                        dt = datetime.strptime(expiry_db, "%d-%b-%y")
                        expiry_api = dt.strftime("%d-%b-%Y").upper()  # 12-MAY-2026
                    except ValueError:
                        expiry_api = expiry_db
                else:
                    expiry_api = "-"

                # Strike: store as float in DB; format with 2 decimals
                strike_val = row.strike or 0
                strike_api = f"{float(strike_val):.2f}"

                # opttype: CE, PE → CE/PE; FUT → XX or "-"
                if instrumenttype in ("CE", "PE"):
                    opttype = instrumenttype
                elif instrumenttype == "FUT":
                    opttype = "-"  # CMOTS uses XX but examples show "-" is fine for FUT
                else:
                    opttype = "-"

                return {
                    "underlying": underlying,
                    "expiry_api": expiry_api,
                    "strike_api": strike_api,
                    "opttype": opttype,
                    "brexchange": brexchange,
                    "instrumenttype": instrumenttype,
                }
        except Exception as e:
            logger.error(f"Error resolving FNO details for {symbol}/{exchange}: {e}")
            return None

    def _build_cmots_payload(
        self,
        symbol: str,
        exchange: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> dict | None:
        """
        Build the JSON payload dict for the CMOTS TradingViewData_AllAssetNew endpoint.

        exchange values from OpenAlgo: NSE, BSE, NFO, BFO, CDS, MCX, NSE_INDEX, BSE_INDEX
        """
        # Determine asset class and exchange string for the payload
        if exchange in ("NSE", "BSE", "NSE_INDEX", "BSE_INDEX"):
            # Equity / Index
            co_code = get_token(symbol, exchange) or "0"
            # For indices, token may not exist – use 0
            if "INDEX" in exchange:
                api_exchange = "NSE" if "NSE" in exchange else "BSE"
                asset = "INDEX"
                sym_name = symbol.upper()
                co_code = "0"
            else:
                api_exchange = exchange
                asset = "EQ"
                # symbol name for the API is the underlying pSymbolName stored as `symbol` in DB
                # but for EQ we can just pass the OpenAlgo symbol (ITC, RELIANCE …)
                sym_name = symbol.upper()
            return {
                "co_code": str(co_code),
                "exchange": api_exchange,
                "fromdate": from_date,
                "todate": to_date,
                "interval": interval,
                "Asset": asset,
                "Symbol": sym_name,
                "expirydate": "-",
                "strikeprice": "0",
                "opttype": "-",
                "option": "Interval",
                "candleLimit": "400",
            }

        elif exchange in ("NFO", "BFO", "CDS", "MCX"):
            # Derivatives
            details = self._resolve_fno_details(symbol, exchange)
            if not details:
                return None

            api_exchange = "NSE"  # default
            if exchange in ("NFO", "CDS"):
                api_exchange = "NSE"
            elif exchange in ("BFO",):
                api_exchange = "BSE"
            elif exchange == "MCX":
                api_exchange = "MCX"

            # Determine asset type
            if details["instrumenttype"] == "FUT":
                asset = "FUT"
                opttype = "-"
            else:
                asset = "FNO"
                opttype = details["opttype"]

            return {
                "co_code": "0",
                "exchange": api_exchange,
                "fromdate": from_date,  # Pass actual date so API returns only the requested range
                "todate": to_date,
                "interval": interval,
                "Asset": asset,
                "Symbol": details["underlying"],
                "expirydate": details["expiry_api"],
                "strikeprice": details["strike_api"] if details["instrumenttype"] != "FUT" else "0",
                "opttype": opttype,
                "option": "Interval",
                "candleLimit": "400",
            }
        else:
            logger.error(f"Unsupported exchange for CMOTS history: {exchange}")
            return None

    def _fetch_cmots_candles(self, payload: dict) -> list[dict]:
        """
        Encode the payload as base64, call the CMOTS endpoint, and return raw result dict.
        Returns the 'result' dict on success, empty dict otherwise.
        """
        payload_json = json.dumps(payload, separators=(",", ":"))
        encoded = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
        url = f"{self.cmots_base_url}{encoded}"

        logger.info(f"CMOTS HISTORY - Fetching: {url[:120]}...")
        logger.debug(f"CMOTS HISTORY - Payload: {payload_json}")

        client = get_httpx_client()
        try:
            response = client.get(url, timeout=30)
            logger.info(f"CMOTS HISTORY - Status: {response.status_code}")
            if response.status_code != 200:
                logger.warning(f"CMOTS HISTORY - Non-200: {response.text[:300]}")
                return {}
            data = response.json()
            if data.get("status") != "SUCCESS":
                logger.warning(f"CMOTS HISTORY - API status not SUCCESS: {data}")
                return {}
            return data.get("result", {})
        except httpx.HTTPError as e:
            logger.error(f"CMOTS HISTORY - HTTP error: {e}")
            return {}
        except Exception as e:
            logger.error(f"CMOTS HISTORY - Error: {e}")
            return {}

    @staticmethod
    def _parse_cmots_timestamp(ts_str: str) -> int:
        """
        Parse CMOTS timestamp string to Unix epoch (seconds).
        Formats seen: "08-May-2026 15:28:00"
        For daily/weekly/monthly candles the format may be "08-May-2026" only.
        """
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
            try:
                dt = datetime.strptime(ts_str.strip(), fmt)
                return int(dt.timestamp())
            except ValueError:
                continue
        logger.warning(f"CMOTS HISTORY - Cannot parse timestamp: {ts_str!r}")
        return 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_history(
        self, symbol: str, exchange: str, interval: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV candles using the CMOTS TradingViewData_AllAssetNew
        endpoint discovered via Kotak web UI.

        Args:
            symbol    : OpenAlgo symbol (e.g. ITC, NIFTY25MAY24000CE)
            exchange  : OpenAlgo exchange (NSE, BSE, NFO, BFO, CDS, MCX, NSE_INDEX, BSE_INDEX)
            interval  : OpenAlgo interval string (1m, 5m, 15m, 30m, 60m, 1h, D, W, M …)
            start_date: "YYYY-MM-DD"
            end_date  : "YYYY-MM-DD"

        Returns:
            pd.DataFrame with columns [timestamp, open, high, low, close, volume]
            timestamp is Unix epoch in seconds (int64).
        """
        empty_df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        # Map interval
        cmots_interval = self.timeframe_map.get(interval)
        if not cmots_interval:
            logger.error(f"CMOTS HISTORY - Unsupported interval: {interval}")
            return empty_df

        try:
            payload = self._build_cmots_payload(
                symbol, exchange, cmots_interval, start_date, end_date
            )
            if payload is None:
                logger.error(f"CMOTS HISTORY - Could not build payload for {symbol}/{exchange}")
                return empty_df

            result = self._fetch_cmots_candles(payload)
            if not result:
                logger.warning(f"CMOTS HISTORY - Empty result for {symbol}/{exchange}")
                return empty_df

            # Unpack parallel arrays
            timestamps = result.get("timestamparray", [])
            opens      = result.get("openpricearray", [])
            highs      = result.get("highpricearray", [])
            lows       = result.get("lowpricearray", [])
            closes     = result.get("closepricearray", [])
            volumes    = result.get("volumearray", [])

            if not timestamps:
                logger.info(f"CMOTS HISTORY - No candles returned for {symbol}/{exchange}")
                return empty_df

            rows = []
            for i, ts_str in enumerate(timestamps):
                epoch = self._parse_cmots_timestamp(ts_str)
                rows.append({
                    "timestamp": epoch,
                    "open":      float(opens[i])   if i < len(opens)   else 0.0,
                    "high":      float(highs[i])   if i < len(highs)   else 0.0,
                    "low":       float(lows[i])    if i < len(lows)    else 0.0,
                    "close":     float(closes[i])  if i < len(closes)  else 0.0,
                    "volume":    int(volumes[i])   if i < len(volumes) else 0,
                })

            df = pd.DataFrame(rows)
            df = (
                df.sort_values("timestamp")
                  .drop_duplicates(subset=["timestamp"])
                  .reset_index(drop=True)
            )
            df["volume"] = df["volume"].astype(int)
            logger.info(
                f"CMOTS HISTORY - Fetched {len(df)} candles for {symbol}/{exchange} [{interval}]"
            )
            return df

        except Exception as e:
            logger.exception(f"CMOTS HISTORY - Unexpected error for {symbol}/{exchange}: {e}")
            return empty_df

    def get_supported_intervals(self) -> dict:
        """Return supported intervals in the format expected by OpenAlgo intervals.py"""
        return {
            "seconds": [],
            "minutes": ["1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "75m", "120m", "125m", "180m"],
            "hours":   ["1h"],
            "days":    ["D"],
            "weeks":   ["W"],
            "months":  ["M"],
        }
