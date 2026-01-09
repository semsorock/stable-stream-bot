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

# Global database connection
db_conn = None


def get_db_connection():
    global db_conn
    if db_conn is None or db_conn.closed != 0:
        logging.info("Opening new database connection")
        db_conn = psycopg2.connect(DB_SYNC_URL)
    return db_conn


# https://cexplorer.io/policy/c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad
POLICY_ID = "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad"

# sample body request
# https://blockfrost.dev/start-building/webhooks/webhooks-events#block
# {
#   "id": "47668401-c3a4-42d4-bac1-ad46515924a3",
#   "webhook_id": "cf68eb9c-635f-415e-a5a8-6233638f28d7",
#   "created": 1650013856,
#   "api_version": 1,
#   "type": "block",
#   "payload": {
#     "time": 1650013853,
#     "height": 7126256,
#     "hash": "f49521b67b440e5030adf124aee8f88881b7682ba07acf06c2781405b0f806a4",
#     "slot": 58447562,
#     "epoch": 332,
#     "epoch_slot": 386762,
#     "slot_leader": "pool1njjr0zn7uvydjy8067nprgwlyxqnznp9wgllfnag24nycgkda25",
#     "size": 34617,
#     "tx_count": 13,
#     "output": "13403118309871",
#     "fees": "4986390",
#     "block_vrf": "vrf_vk197w95j9alkwt8l4g7xkccknhn4pqwx65c5saxnn5ej3cpmps72msgpw69d",
#     "previous_block": "9e3f5bfc9f0be44cf6e14db9ed5f1efb6b637baff0ea1740bb6711786c724915",
#     "next_block": null,
#     "confirmations": 0
#   }
# }


def format_pretty_string(number):
    formatted_number = "{:,.2f}".format(number)
    return formatted_number


GET_MINTS = """
    SELECT encode(tx.hash, 'hex'), mtm.quantity
      FROM ma_tx_mint AS mtm
      JOIN multi_asset AS ma ON mtm.ident = ma.id 
      JOIN tx ON tx.id = mtm.tx_id
      JOIN block ON block.id = tx.block_id
     WHERE encode(ma.policy, 'hex') = %s
       AND encode(block.hash, 'hex') = %s"""

GET_SUPPLY = """
    SELECT sum(mtm.quantity) FROM ma_tx_mint AS mtm 
      JOIN multi_asset AS ma ON mtm.ident = ma.id 
     WHERE encode(ma.policy, 'hex') = %s"""


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


def process_transaction(tx_hash, mint, supply):
    logging.info(f"tx_hash: {tx_hash}, mint: {mint}, supply: {supply}")

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
            f"https://cardanoscan.io/transaction/{tx_hash}?tab=tokenmint\n",
            f"#cardano #blockchain #stablecoin",
        ]
    )

    logging.info(tweet_text)

    response = client.create_tweet(text=tweet_text)
    logging.info(f"Response: {response}")


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

    try:
        conn = get_db_connection()
        # Get total supply once per webhook call
        supply = get_one(conn, GET_SUPPLY, (POLICY_ID,))
        logging.info(f"Current total supply: {supply}")

        # Expecting payload type 'block'
        # Webhook documentation: https://blockfrost.dev/start-building/webhooks/webhooks-events#block
        payload = request_json.get("payload", {})
        block_hash = payload.get("hash")

        if block_hash:
            logging.info(f"Processing block: {block_hash}")

            # Query all relevant mints from that block at once
            cur = conn.cursor()
            cur.execute(GET_MINTS, (POLICY_ID, block_hash))
            rows = cur.fetchall()
            cur.close()

            logging.info(f"Found {len(rows)} relevant mints in block {block_hash}")

            for tx_hash, mint in rows:
                process_transaction(tx_hash, mint, supply)
        else:
            logging.warning("No block hash found in payload")

    except Exception as e:
        logging.error(f"Error processing request: {e}")
        # If there's a psycopg2 Error, we might want to clear the global connection
        if isinstance(e, psycopg2.Error):
            global db_conn
            db_conn = None


    return f"Received POST data: {request_json}".encode("utf-8")
