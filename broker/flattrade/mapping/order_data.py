import json

from database.token_db import get_oa_symbol, get_symbol
from utils.logging import get_logger

logger = get_logger(__name__)


def map_order_data(order_data):
    """
    Processes and modifies a list of order dictionaries based on specific conditions.

    Parameters:
    - order_data: A list of dictionaries, where each dictionary represents an order.

    Returns:
    - The modified order_data with updated 'tradingsymbol' and 'product' fields.
    """
    # Check if 'data' is None
    if order_data is None or (isinstance(order_data, dict) and (order_data["stat"] == "Not_Ok")):
        # Handle the case where there is no data
        logger.warning("No data available.")
        order_data = {}  # or set it to an empty list if it's supposed to be a list
    else:
        order_data = order_data

    if order_data:
        for order in order_data:
            # Extract the instrument_token and exchange for the current order
            symboltoken = order["token"]
            exchange = order["exch"]

            # Use the get_symbol function to fetch the symbol from the database
            symbol_from_db = get_symbol(symboltoken, exchange)

            # Check if a symbol was found; if so, update the trading_symbol in the current order
            if symbol_from_db:
                order["tsym"] = symbol_from_db
                if (order["exch"] == "NSE" or order["exch"] == "BSE") and order["prd"] == "C":
                    order["prd"] = "CNC"

                elif order["prd"] == "I":
                    order["prd"] = "MIS"

                elif order["exch"] in ["NFO", "MCX", "BFO", "CDS"] and order["prd"] == "M":
                    order["prd"] = "NRML"

                if order["prctyp"] == "MKT":
                    order["prctyp"] = "MARKET"
                elif order["prctyp"] == "LMT":
                    order["prctyp"] = "LIMIT"
                elif order["prctyp"] == "SL-MKT":
                    order["prctyp"] = "SL-M"
                elif order["prctyp"] == "SL-LMT":
                    order["prctyp"] = "SL"

                # 🔥 NEW: Use avgprc if instname and avgprc are present (highest priority)
                if order.get("instname") and order.get("avgprc"):
                    avgprc = order.get("avgprc", 0)
                    if avgprc and float(avgprc) > 0:
                        order["prc"] = avgprc
                        logger.debug(
                            f"Updated price from avgprc for order with instname: {order.get('norenordno', '')} - Price: {avgprc}"
                        )

                # 🔥 EXISTING: Price logic for MARKET and SL-M orders (fallback)
                elif order["prctyp"] in ["MARKET", "SL-M"] and float(order.get("prc", 0)) == 0.0:
                    rprc = order.get("rprc", 0)
                    if rprc and float(rprc) > 0:
                        order["prc"] = rprc
                        logger.debug(
                            f"Updated price from rprc for {order['prctyp']} order: {order.get('norenordno', '')}"
                        )

            else:
                logger.warning(
                    f"Symbol not found for token {symboltoken} and exchange {exchange}. Keeping original trading symbol."
                )

    return order_data


def calculate_order_statistics(order_data):
    """
    Calculates statistics from order data, including totals for buy orders, sell orders,
    completed orders, open orders, and rejected orders.

    Parameters:
    - order_data: A list of dictionaries, where each dictionary represents an order.

    Returns:
    - A dictionary containing counts of different types of orders.
    """
    # Initialize counters
    total_buy_orders = total_sell_orders = 0
    total_completed_orders = total_open_orders = total_rejected_orders = 0

    if order_data:
        for order in order_data:
            # Count buy and sell orders
            if order["trantype"] == "B":
                order["trantype"] = "BUY"
                total_buy_orders += 1
            elif order["trantype"] == "S":
                order["trantype"] = "SELL"
                total_sell_orders += 1

            # Count orders based on their status
            if order["status"] == "COMPLETE":
                total_completed_orders += 1
            elif order["status"] == "OPEN":
                total_open_orders += 1
            elif order["status"] == "REJECTED":
                total_rejected_orders += 1

    # Compile and return the statistics
    return {
        "total_buy_orders": total_buy_orders,
        "total_sell_orders": total_sell_orders,
        "total_completed_orders": total_completed_orders,
        "total_open_orders": total_open_orders,
        "total_rejected_orders": total_rejected_orders,
    }


def transform_order_data(orders):
    # Handle None or empty orders
    if orders is None:
        logger.warning("No order data available - orders is None")
        return []

    if not orders:
        logger.info("No orders found - empty list")
        return []

    transformed_orders = []

    for order in orders:
        # Make sure each item is indeed a dictionary
        if not isinstance(order, dict):
            logger.warning(
                f"Warning: Expected a dict, but found a {type(order)}. Skipping this item."
            )
            continue

        transformed_order = {
            "symbol": order.get("tsym", ""),
            "exchange": order.get("exch", ""),
            "action": order.get("trantype", ""),
            "quantity": order.get("qty", 0),
            "price": order.get("prc", 0.0),
            "trigger_price": order.get("trgprc", 0.0),
            "pricetype": order.get("prctyp", ""),
            "product": order.get("prd", ""),
            "orderid": order.get("norenordno", ""),
            "order_status": order.get("status", "").lower(),
            "timestamp": order.get("norentm", ""),
        }

        transformed_orders.append(transformed_order)

    return transformed_orders


def map_trade_data(trade_data):
    """
    Processes and modifies a list of order dictionaries based on specific conditions.

    Parameters:
    - order_data: A list of dictionaries, where each dictionary represents an order.

    Returns:
    - The modified order_data with updated 'tradingsymbol' and 'product' fields.
    """
    # Check if 'data' is None
    if trade_data is None or (isinstance(trade_data, dict) and (trade_data["stat"] == "Not_Ok")):
        # Handle the case where there is no data
        # For example, you might want to display a message to the user
        # or pass an empty list or dictionary to the template.
        logger.warning("No data available.")
        trade_data = {}  # or set it to an empty list if it's supposed to be a list
    else:
        trade_data = trade_data

    if trade_data:
        for order in trade_data:
            # Extract the instrument_token and exchange for the current order
            symbol = order["tsym"]
            exchange = order["exch"]

            # Use the get_symbol function to fetch the symbol from the database
            symbol_from_db = get_oa_symbol(symbol, exchange)

            # Check if a symbol was found; if so, update the trading_symbol in the current order
            if symbol_from_db:
                order["tsym"] = symbol_from_db
                if (order["exch"] == "NSE" or order["exch"] == "BSE") and order["prd"] == "C":
                    order["prd"] = "CNC"

                elif order["prd"] == "I":
                    order["prd"] = "MIS"

                elif order["exch"] in ["NFO", "MCX", "BFO", "CDS"] and order["prd"] == "M":
                    order["prd"] = "NRML"

                if order["trantype"] == "B":
                    order["trantype"] = "BUY"
                elif order["trantype"] == "S":
                    order["trantype"] = "SELL"

            else:
                logger.warning(
                    f"Unable to find the symbol {symbol} and exchange {exchange}. Keeping original trading symbol."
                )

    return trade_data


def transform_tradebook_data(tradebook_data):
    transformed_data = []
    for trade in tradebook_data:
        # Format numeric values to 2 decimal places for tradebook only
        avg_price = round(float(trade.get("avgprc", 0)), 2)
        quantity = int(trade.get("qty", 0))
        trade_value = round(avg_price * quantity, 2)

        transformed_trade = {
            "symbol": trade.get("tsym", ""),
            "exchange": trade.get("exch", ""),
            "product": trade.get("prd", ""),
            "action": trade.get("trantype", ""),
            "quantity": quantity,
            "average_price": avg_price,
            "trade_value": trade_value,
            "orderid": trade.get("norenordno", ""),
            "timestamp": trade.get("norentm", ""),
        }
        transformed_data.append(transformed_trade)
    return transformed_data


def map_position_data(position_data):
    """
    Processes and modifies a list of position dictionaries based on specific conditions.

    Parameters:
    - position_data: A list of dictionaries, where each dictionary represents a position.

    Returns:
    - The modified position_data with updated 'tradingsymbol' and 'product' fields.
    """
    if position_data is None or (
        isinstance(position_data, dict) and (position_data["stat"] == "Not_Ok")
    ):
        # Handle the case where there is no data
        logger.warning("No data available.")
        position_data = {}  # or set it to an empty list if it's supposed to be a list
    else:
        position_data = position_data

    if position_data:
        for order in position_data:
            # Extract the instrument_token and exchange for the current order
            symbol = order["tsym"]
            exchange = order["exch"]

            # Use the get_symbol function to fetch the symbol from the database
            symbol_from_db = get_oa_symbol(symbol, exchange)

            # Check if a symbol was found; if so, update the trading_symbol in the current order
            if symbol_from_db:
                order["tsym"] = symbol_from_db
                if (order["exch"] == "NSE" or order["exch"] == "BSE") and order["prd"] == "C":
                    order["prd"] = "CNC"

                elif order["prd"] == "I":
                    order["prd"] = "MIS"

                elif order["exch"] in ["NFO", "MCX", "BFO", "CDS"] and order["prd"] == "M":
                    order["prd"] = "NRML"
            else:
                logger.warning(
                    f"Unable to find the symbol {symbol} and exchange {exchange}. Keeping original trading symbol."
                )

    return position_data


def transform_positions_data(positions_data):
    """
    Transforms position data for display in the positions page.

    Parameters:
    - positions_data: A list of dictionaries with position data

    Returns:
    - A list of transformed position dictionaries with calculated P&L fields
    """
    transformed_data = []
    for position in positions_data:
        # Calculate P&L using broker-provided values
        realized_pnl = float(position.get("rpnl", 0))
        unrealized_pnl = float(position.get("urmtom", 0))

        # Fallback calculation if broker values aren't available
        if unrealized_pnl == 0 and float(position.get("netqty", 0)) != 0:
            price_factor = float(position.get("prcftr", 1))
            ltp = float(position.get("lp", 0))
            avg_price = float(position.get("netavgprc", 0))
            quantity = float(position.get("netqty", 0))
            unrealized_pnl = (ltp - avg_price) * quantity * price_factor

        # Calculate total P&L (realized + unrealized)
        total_pnl = realized_pnl + unrealized_pnl

        transformed_position = {
            "symbol": position.get("tsym", ""),
            "exchange": position.get("exch", ""),
            "product": position.get("prd", ""),
            "quantity": position.get("netqty", 0),
            "average_price": position.get("netavgprc", 0.0),
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "ltp": position.get("lp", 0.0),
            "pnl": round(total_pnl, 2),  # Combined P&L for display in positions table
        }
        transformed_data.append(transformed_position)
    return transformed_data


def map_portfolio_data(portfolio_data, auth_token=None):
    """
    Processes and modifies a list of Portfolio dictionaries based on specific conditions and
    ensures both holdings and totalholding parts are transmitted in a single response.

    Parameters:
    - portfolio_data: A list of dictionaries, where each dictionary represents portfolio information.
    - auth_token: Direct broker authentication token (optional).

    Returns:
    - The modified portfolio_data with 'product' fields changed for 'holdings' and 'totalholding' included.
    """
    # Check if 'portfolio_data' is a list
    if not portfolio_data or not isinstance(portfolio_data, list):
        logger.info("No data available or incorrect data format.")
        return []

    # Map symbols and collect them for LTP fetching
    symbols_payload = []
    for portfolio in portfolio_data:
        # Ensure 'stat' is 'Ok' before proceeding
        if portfolio.get("stat") != "Ok":
            logger.info(f"Error: {portfolio.get('emsg', 'Unknown error occurred.')}")
            continue

        # Process the 'exch_tsym' list inside each portfolio entry
        for exch_tsym in portfolio.get("exch_tsym", []):
            symbol = exch_tsym.get("tsym", "")
            exchange = exch_tsym.get("exch", "")

            # Replace 'get_oa_symbol' function with your actual symbol fetching logic
            symbol_from_db = get_oa_symbol(symbol, exchange)

            if symbol_from_db:
                exch_tsym["tsym"] = symbol_from_db
            else:
                logger.info(
                    f"Flattrade Portfolio - Product Value for {symbol} Not Found or Changed."
                )

            # Store resolved info and schedule for LTP lookup
            symbols_payload.append({
                "symbol": exch_tsym["tsym"],
                "exchange": exchange,
                "ref": exch_tsym
            })

    # Resolve auth_token from request context if not provided
    if not auth_token:
        try:
            from flask import request
            if request and request.is_json and request.json:
                api_key = request.json.get("apikey")
                if api_key:
                    from database.auth_db import get_auth_token_broker
                    auth_token, _ = get_auth_token_broker(api_key)
        except Exception as ex:
            logger.debug(f"Could not get api key from request: {ex}")

    # Fallback to database query if still not resolved
    if not auth_token:
        try:
            from database.auth_db import ApiKeys, decrypt_token, get_auth_token_broker
            api_key_obj = ApiKeys.query.first()
            if api_key_obj:
                api_key = decrypt_token(api_key_obj.api_key_encrypted)
                auth_token, _ = get_auth_token_broker(api_key)
        except Exception as e:
            logger.warning(f"Failed to fetch api key from database fallback: {e}")

    # Fetch LTPs sequentially to avoid eventlet ThreadPoolExecutor freezing/deadlocking
    if auth_token and symbols_payload:
        try:
            from broker.flattrade.api.data import BrokerData
            data_handler = BrokerData(auth_token)
            for s in symbols_payload:
                try:
                    quote = data_handler.get_quotes(s["symbol"], s["exchange"])
                    if quote and "ltp" in quote:
                        s["ref"]["_ltp"] = float(quote["ltp"])
                except Exception as ex:
                    logger.warning(f"Failed to fetch quote for {s['symbol']}: {ex}")
        except Exception as e:
            logger.warning(f"Failed to initialize BrokerData or fetch quotes: {e}")

    return portfolio_data


def calculate_portfolio_statistics(holdings_data):
    totalholdingvalue = 0
    totalinvvalue = 0
    totalprofitandloss = 0
    totalpnlpercentage = 0

    # Check if the data is valid or contains an error
    if not holdings_data or not isinstance(holdings_data, list):
        logger.info("Error: Invalid or missing holdings data.")
        return {
            "totalholdingvalue": totalholdingvalue,
            "totalinvvalue": totalinvvalue,
            "totalprofitandloss": totalprofitandloss,
            "totalpnlpercentage": totalpnlpercentage,
        }

    # Iterate over the list of holdings
    for holding in holdings_data:
        # Ensure 'stat' is 'Ok' before proceeding
        if holding.get("stat") != "Ok":
            logger.info(f"Error: {holding.get('emsg', 'Unknown error occurred.')}")
            continue

        # Filter out the NSE entry and ignore BSE for the same symbol
        nse_entry = next(
            (exch for exch in holding.get("exch_tsym", []) if exch.get("exch") == "NSE"), None
        )
        if not nse_entry:
            continue  # Skip if no NSE entry is found

        # Process only the NSE entry
        # Using npoadqty as per Flattrade documentation for Non Poa display quantity
        quantity = float(holding.get("holdqty", 0)) + max(
            float(holding.get("npoadqty", 0)), float(holding.get("dpqty", 0))
        )
        upload_price = float(holding.get("upldprc", 0))
        
        # Get the fetched LTP or fall back to upload_price (average cost) so P&L is 0 if quotes fail
        market_price = float(nse_entry.get("_ltp", 0.0) or 0.0) or upload_price

        # Calculate investment value and holding value for NSE
        inv_value = quantity * upload_price
        holding_value = quantity * market_price
        profit_and_loss = holding_value - inv_value

        # Accumulate the totals
        totalinvvalue += inv_value
        totalholdingvalue += holding_value
        totalprofitandloss += profit_and_loss

    # Calculate overall P&L percentage
    totalpnlpercentage = (totalprofitandloss / totalinvvalue) * 100 if totalinvvalue != 0 else 0

    return {
        "totalholdingvalue": totalholdingvalue,
        "totalinvvalue": totalinvvalue,
        "totalprofitandloss": totalprofitandloss,
        "totalpnlpercentage": totalpnlpercentage,
    }


def transform_holdings_data(holdings_data):
    transformed_data = []
    if isinstance(holdings_data, list):
        for holding in holdings_data:
            # Filter out only NSE exchange
            nse_entries = [
                exch for exch in holding.get("exch_tsym", []) if exch.get("exch") == "NSE"
            ]
            for exch_tsym in nse_entries:
                quantity = int(holding.get("holdqty", 0)) + max(
                    int(holding.get("npoadqty", 0)), int(holding.get("dpqty", 0))
                )
                avg_price = float(holding.get("upldprc", 0.0))
                ltp = float(exch_tsym.get("_ltp", 0.0) or 0.0)
                
                if ltp > 0:
                    pnl = (ltp - avg_price) * quantity
                    pnlpercent = (pnl / (avg_price * quantity) * 100) if (avg_price and quantity) else 0.0
                else:
                    pnl = 0.0
                    pnlpercent = 0.0

                transformed_position = {
                    "symbol": exch_tsym.get("tsym", ""),
                    "exchange": exch_tsym.get("exch", ""),
                    "quantity": quantity,
                    "product": exch_tsym.get("product", "CNC"),
                    "average_price": avg_price,
                    "pnl": pnl,
                    "pnlpercent": pnlpercent,
                }
                transformed_data.append(transformed_position)
    return transformed_data
