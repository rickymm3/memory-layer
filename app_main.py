"""Entry point for the hosted memory-layer public site.

Local dev (admin dashboard):  make dashboard   → dashboard/app.py on port 5001
Hosted public site:           make site        → this file on port 5000
Production deploy:            gunicorn 'app_main:create_app()'
"""
from __future__ import annotations

import os

from flask import Flask
from flask_login import current_user

from webapp.auth import login_manager
from webapp.routes import site_bp, _unread_notification_count


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)

    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set. "
            "Add it to .env or your hosting environment."
        )
    app.secret_key = secret
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    login_manager.init_app(app)
    app.register_blueprint(site_bp)

    @app.context_processor
    def inject_notifications():
        if current_user.is_authenticated:
            return {"unread_count": _unread_notification_count(current_user.username)}
        return {"unread_count": 0}

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    create_app().run(port=5000, debug=True)
