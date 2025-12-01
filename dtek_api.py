
# dtek_api.py
import os
from flask import Flask

app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "OK", 200

# simple health endpoint to show status (можна розширити)
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)