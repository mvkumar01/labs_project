"""
Labs Flask application entry point.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from flask import Flask, redirect, url_for

from storage.db import init_db
from labs.ui.routes import labs_bp

app = Flask(__name__)
app.secret_key = "labs-dev-secret-change-in-prod"

app.register_blueprint(labs_bp)


@app.route("/")
def index():
    return redirect(url_for("labs.dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
