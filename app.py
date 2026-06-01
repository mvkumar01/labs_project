"""
Labs Flask application entry point.
"""
import os
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from flask import Flask, redirect, url_for

from storage.db import init_db
from storage.live_db import init_live_db
from labs.ui.routes import labs_bp
from labs.ui.live_routes import live_bp
from live.auth_gate import register_auth_gate
app = Flask(__name__)
# 32-byte hex from PA env (LABS_SECRET_KEY); ephemeral fallback for local dev.
app.secret_key = os.environ.get("LABS_SECRET_KEY") or secrets.token_hex(32)

# WSGI imports the module directly, so initialize both schemas on import.
init_db()
init_live_db()

app.register_blueprint(labs_bp)

# Live real-money stack (parallel, import-isolated). The auth gate covers the
# /live blueprint only; /labs is untouched. Routes mutate DB/config only.
register_auth_gate(app)
app.register_blueprint(live_bp)


@app.route("/")
def index():
    return redirect(url_for("labs.dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
