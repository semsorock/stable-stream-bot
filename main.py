import functions_framework # type: ignore
import os
import tweepy
import psycopg2
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)

API_KEY = os.environ.get("API_KEY")
API_SECRET_KEY = os.environ.get("API_SECRET_KEY")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.environ.get("ACCESS_TOKEN_SECRET")
DB_SYNC_URL = os.environ.get("DB_SYNC_URL")

client = tweepy.Client(
    consumer_key=API_KEY,
    consumer_secret=API_SECRET_KEY,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_TOKEN_SECRET,
)

# https://cexplorer.io/policy/c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad
POLICY_ID = "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad"

# sample body request
# {
#   "id": "a11c6d90-7bd5-4c0d-9aba-eb4ea3cafd5b",
#   "webhook_id": "c87b86d9-696f-475b-b5ee-3fa5c80e9639",
#   "created": 1741604600,
#   "api_version": 1,
#   "type": "transaction",
#   "payload": [
#     {
#       "tx": {
#         "hash": "de307385c92aaab8fbd9d3862c03684f90646876a126b2c6d08805c8745de85c",
#         "block": "756bcc96673889de2a013d59962cb4b30ed321a2090c616f976382c40260bedb",
#         "block_height": 11584723,
#         "block_time": 1741604523,
#         "slot": 150038232,
#         "index": 3,
#         "output_amount": [],
#         "fees": "529003",
#         "deposit": "0",
#         "size": 1383,
#         "invalid_before": "150038217",
#         "invalid_hereafter": "150039217",
#         "utxo_count": 6,
#         "withdrawal_count": 1,
#         "mir_cert_count": 0,
#         "delegation_count": 0,
#         "stake_cert_count": 0,
#         "pool_update_count": 0,
#         "pool_retire_count": 0,
#         "asset_mint_or_burn_count": 1,
#         "redeemer_count": 4,
#         "valid_contract": true
#       },
#       "inputs": [

#       ],
#       "outputs": [
#       ]
#     }
#   ]
# }


def format_pretty_string(number):
    formatted_number = "{:,.2f}".format(number)
    return formatted_number


GET_MINT = """
    SELECT mtm.quantity FROM ma_tx_mint AS mtm
      JOIN multi_asset AS ma ON mtm.ident = ma.id 
      JOIN tx on tx.id = mtm.tx_id
     WHERE ma.fingerprint = 'asset12ffdj8kk2w485sr7a5ekmjjdyecz8ps2cm5zed'
       AND encode(tx.hash::bytea, 'hex') = %s"""

GET_SUPPLY = """
    SELECT sum(mtm.quantity) FROM ma_tx_mint AS mtm 
      JOIN multi_asset AS ma ON mtm.ident = ma.id 
     WHERE ma.fingerprint = 'asset12ffdj8kk2w485sr7a5ekmjjdyecz8ps2cm5zed'"""


def get_one(conn, query, params: tuple = None):
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        row = cur.fetchone()
        result = None if row is None else row[0]
    except Exception as e:
        logging.error(f"Error executing query: {e}")
        result = None
    finally:
        cur.close()
    return result


def process_transaction(conn, tx_hash):
    mint = get_one(conn, GET_MINT, (tx_hash,))
    logging.info(f"mint: {mint}")

    if mint is not None:
        supply = get_one(conn, GET_SUPPLY)
        logging.info(f"supply: {supply}")

        action = ""
        pretty_quantity = format_pretty_string(mint / 1_000_000)

        if mint > 0:
            action = "MINTED"
            pretty_quantity = "+" + pretty_quantity
        elif mint < 0:
            action = "BURNED"

        pretty_supply = format_pretty_string(supply / 1_000_000)

        tweet_text = "\n".join(
            [
                f"$USDM has been {action}!",
                f"quantity: {pretty_quantity}",
                f"total supply: {pretty_supply}",
                f"https://cexplorer.io/tx/{tx_hash}/mint#data\n",
                f"#cardano #blockchain #stablecoin",
            ]
        )

        logging.info(tweet_text)

        response = client.create_tweet(text=tweet_text)
        logging.info(f"Response: {response}")
    else:
        logging.info(f"Skipping. Policy there is no USDM mint in tx: {tx_hash}")


@functions_framework.http
def hello_http(request):
    """HTTP Cloud Function.
    Args:
        request (flask.Request): The request object.
        <https://flask.palletsprojects.com/en/1.1.x/api/#incoming-request-data>
    Returns:
        The response text, or any set of values that can be turned into a
        Response object using `make_response`
        <https://flask.palletsprojects.com/en/1.1.x/api/#flask.make_response>.
    """
    request_json = request.get_json(silent=True)
    request_args = request.args

    logging.info(f"request_json: {request_json}")

    conn = psycopg2.connect(DB_SYNC_URL)
    try:
        for item in request_json.get("payload", []):
            tx_hash = item.get("tx", {}).get("hash")
            process_transaction(conn, tx_hash)

    except Exception as e:
        logging.error(f"Error processing request: {e}")
    finally:
        conn.close()

    return f"Received POST data: {request_json}".encode("utf-8")
