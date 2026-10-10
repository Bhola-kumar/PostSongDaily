import logging

from flask import Flask, jsonify, render_template

from .api import bp as api_bp
from .config import Config
from .jobs import JobStore


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Flask(__name__)
    Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app.config["VG"] = Config
    app.extensions["jobs"] = JobStore(Config)
    app.register_blueprint(api_bp)

    @app.get("/")
    def index():
        if Config.API_KEY:
            return jsonify({"service": "videogen", "health": "/api/healthz",
                            "submit": "POST /api/jobs", "status": "GET /api/jobs/{job_id}",
                            "batch_status": "GET /api/batches/{batch_id}",
                            "authentication": "Authorization: Bearer <API_KEY>"})
        return render_template("index.html")

    return app
