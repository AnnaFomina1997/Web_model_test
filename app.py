from flask import Flask, request, send_file, jsonify
from diffusers import StableDiffusionPipeline
import torch
import warnings
import json
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import requests
import logging
import time

# Suppress specific warnings
warnings.simplefilter("ignore", category=FutureWarning)

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Initialize Flask app
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:postgres@localhost:5002/postgres'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
CORS(app)

# Initialize diffusion pipeline
model_id = "runwayml/stable-diffusion-v1-5"
pipe = StableDiffusionPipeline.from_pretrained(model_id, torch_dtype=torch.float16)
pipe = pipe.to("cuda")

# Configuration for TonAPI
TONAPI_BASE_URL = "https://testnet.tonapi.io/v2"
TONAPI_HEADERS = {'Authorization': 'Bearer AF56IZZA67IF4TYAAAAKWMYN5B5TBHIZP6VCSME5IPPDDI7SVHB3GF52JA36DYV4DTHH3DI'}


class userTable(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    ton_balance = db.Column(db.Float, nullable=False, default=0.0)
    account_balance = db.Column(db.Float, nullable=False, default=0.0)

    def __repr__(self):
        return f'<userTable {self.username}>'

    def update_balance(self, amount):
        self.account_balance += amount
        db.session.commit()


@app.route('/generate', methods=["POST"])
def generate_image():
    data = request.json
    prompt = data['prompt']
    username = data['username']

    user = userTable.query.filter_by(username=username).first()

    if not user:
        return jsonify({'error': 'User not found'}), 404

    if user.account_balance < 1:
        return jsonify({'error': 'Insufficient tokens'}), 400

    # Generate image
    image = pipe(prompt).images[0]
    image_path = "generated_image.png"
    image.save(image_path)

    return send_file(image_path, mimetype="image/png")


@app.route('/get_balance', methods=['POST'])
def get_balance():
    address = request.json.get('address')
    api_key = "0b14fb162c2c45bcb956480584ec6dae"  # Securely fetched from environment or config
    endpoint = f"https://go.getblock.io/{api_key}/getAddressBalance?address={address}"
    response = requests.get(endpoint)
    if response.ok:
        balance_data = response.json()
        return jsonify(balance_data)
    else:
        return jsonify({"error": "Failed to retrieve balance"}), 500


@app.route('/get_token_balance', methods=['POST'])
def get_token_balance():
    account_id = request.json.get('account_id')
    token_symbol = request.json.get('token_symbol')

    endpoint = f'{TONAPI_BASE_URL}/accounts/{account_id}/jettons'

    try:
        logging.debug(f"Requesting TONAPI endpoint: {endpoint}")
        response = requests.get(endpoint)  # Без заголовка Authorization
        logging.debug(f"Response status code: {response.status_code}")
        logging.debug(f"Response text: {response.text}")

        if response.status_code == 401:
            return jsonify({'error': 'Unauthorized. Check your TONAPI token.'}), 401

        if response.ok:
            data = response.json()
            balances = data.get('balances', [])
            for jetton in balances:
                if jetton['jetton']['symbol'] == token_symbol:
                    return jsonify({'token_balance': jetton['balance'], 'symbol': token_symbol})
            return jsonify({'error': f'Token {token_symbol} not found'}), 404
        else:
            return jsonify({'error': 'Failed to fetch token data', 'statusCode': response.status_code,
                            'response': response.text}), response.status_code
    except Exception as e:
        logging.error(f"Error accessing TON API: {str(e)}")
        return jsonify({'error': 'Server error', 'message': str(e)}), 500


@app.route('/update_user', methods=['POST'])
def update_user():
    data = request.json
    user = userTable.query.filter_by(username=data['username']).first()
    if user:
        if 'ton_balance' in data and user.ton_balance != data['ton_balance']:
            user.ton_balance = data['ton_balance']
    else:
        user = userTable(
            username=data['username'],
            ton_balance=data.get('ton_balance', 0.0),
            account_balance=data.get('account_balance', 0.0)
        )
        db.session.add(user)

    db.session.commit()
    return jsonify({"message": "User data updated", "data": {"username": user.username, "ton_balance": user.ton_balance,
                                                             "account_balance": user.account_balance}})


@app.route('/get_account_balance', methods=['POST'])
def get_account_balance():
    user_id = request.json.get('user_id')
    user = userTable.query.filter_by(username=user_id).first()
    if user:
        return jsonify({'success': True, 'account_balance': user.account_balance})
    else:
        return jsonify({'success': False, 'message': 'User not found'}), 404


@app.route('/deduct_token', methods=['POST'])
def deduct_token():
    user_id = request.json.get('user_id')
    deduction_amount = request.json.get('deduction_amount', 1)  # Default deduction amount is 1 token

    user = userTable.query.filter_by(username=user_id).first()
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    if user.account_balance >= deduction_amount:
        user.account_balance -= deduction_amount
        db.session.commit()
        return jsonify({'success': True, 'message': 'Token deducted', 'new_balance': user.account_balance})
    else:
        return jsonify({'success': False, 'message': 'Insufficient balance'}), 400


@app.route('/update_balance_after_transaction', methods=['POST'])
def update_balance_after_transaction():
    data = request.json
    user_id = data.get('user_id')
    token_id = data.get('token_id')
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    logging.debug(f"Received request: {data}")

    user = userTable.query.filter_by(username=user_id).first()
    if not user:
        logging.error(f"User {user_id} not found")
        return jsonify({'success': False, 'message': 'User not found'}), 404

    # Период ожидания в секундах и количество попыток
    wait_time = 15
    max_retries = 12
    last_event_id = None
    skip = 1
    passt = False

    for attempt in range(max_retries):
        url = f"{TONAPI_BASE_URL}/accounts/{user_id}/jettons/{token_id}/history?limit=100&start_date={start_date}&end_date={end_date}"
        logging.debug(f"Requesting TonAPI: {url}")
        try:
            response = requests.get(url, headers=TONAPI_HEADERS)
            response.raise_for_status()  # Raise an exception for HTTP errors

            events = response.json().get('events', [])
            logging.debug(f"Received events: {events}")

            if events:
                latest_event = events[0]
                logging.debug(f"Processing latest event: {latest_event['event_id']}")

                if last_event_id is None:
                    last_event_id = latest_event['event_id']
                elif last_event_id != latest_event['event_id']:
                    if skip == 1:
                        time.sleep(wait_time)
                        passt = True
                        skip = 0
                        continue

                if passt:
                    logging.debug(latest_event)
                    if latest_event["actions"][0]["status"] == 'ok':
                        jetton_transfer = latest_event["actions"][0]['JettonTransfer']
                        transaction_amount = int(jetton_transfer['amount'])
                        user.account_balance += transaction_amount / 1e9  # Assuming the amount is in nanocoins
                        db.session.commit()
                        logging.info(f"Updated user balance: {user.account_balance}")
                        return jsonify(
                            {'success': True, 'message': 'Balance updated', 'new_balance': user.account_balance})
                    else:
                        logging.error("No successful JettonTransfer action found")
                        return jsonify({'success': False, 'message': 'No successful JettonTransfer action found'}), 404

                # Reset passt after processing
                passt = False

        except requests.RequestException as e:
            logging.error(f"Request error: {e}")
            return jsonify({'success': False, 'message': 'Error contacting TonAPI'}), 500

        logging.debug(f"No new transactions found on attempt {attempt + 1}/{max_retries}")
        time.sleep(wait_time)

    logging.error(f"Failed to fetch new transaction after {max_retries} attempts")
    return jsonify({'success': False, 'message': 'Failed to fetch new transaction'}), 404


if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
