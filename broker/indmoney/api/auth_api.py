import json
import os

import httpx

from broker.indmoney.api.baseurl import BASE_URL, get_url
from utils.httpx_client import get_httpx_client


def authenticate_broker(access_token):
    try:
        # Clean/strip the token
        token_to_validate = (access_token or "").strip()

        if token_to_validate:
            # Validate token against IndStocks API
            client = get_httpx_client()
            headers = {"Authorization": token_to_validate}
            validation_url = "https://api.indstocks.com/market/instruments?source=equity"
            try:
                resp = client.get(validation_url, headers=headers, timeout=10.0)
                if resp.status_code in (401, 403):
                    return None, "IndMoney access token is expired or invalid (HTTP 403/401). Please enter a valid access token."
                elif resp.status_code != 200:
                    return None, f"IndMoney validation failed with HTTP {resp.status_code}: {resp.text[:100]}"
            except Exception as test_err:
                return None, f"Failed to validate token due to network/server error: {str(test_err)}"

            return token_to_validate, None
        else:
            return None, "Access Token is required"

    except Exception as e:
        return None, f"An exception occurred: {str(e)}"
