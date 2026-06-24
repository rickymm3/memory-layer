"""Flask-Login integration for the memory-layer public site."""
from __future__ import annotations

import os

import psycopg
from flask_login import LoginManager, UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.config import get_config

login_manager = LoginManager()
login_manager.login_view = "webapp.login"
login_manager.login_message = "Please log in to access that page."


class User(UserMixin):
    def __init__(self, id: str, username: str, email: str | None,
                 is_admin: bool, is_active: bool, api_token: str) -> None:
        self.id = id
        self.username = username
        self.email = email
        self._is_admin = is_admin
        self._is_active = is_active
        self.api_token = api_token

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def is_admin(self) -> bool:
        return self._is_admin

    @staticmethod
    def _row_to_user(row) -> "User":
        return User(
            id=str(row[0]),
            username=row[1],
            email=row[2],
            is_admin=bool(row[3]),
            is_active=bool(row[4]),
            api_token=row[5] or "",
        )

    @classmethod
    def get_by_id(cls, user_id: str) -> "User | None":
        try:
            with psycopg.connect(get_config().database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, email, is_admin, is_active, api_token "
                        "FROM users WHERE id = %s;",
                        (user_id,),
                    )
                    row = cur.fetchone()
            return cls._row_to_user(row) if row else None
        except Exception:
            return None

    @classmethod
    def get_by_username(cls, username: str) -> "User | None":
        try:
            with psycopg.connect(get_config().database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, email, is_admin, is_active, api_token "
                        "FROM users WHERE username = %s;",
                        (username,),
                    )
                    row = cur.fetchone()
            return cls._row_to_user(row) if row else None
        except Exception:
            return None

    @classmethod
    def get_by_token(cls, token: str) -> "User | None":
        try:
            with psycopg.connect(get_config().database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, email, is_admin, is_active, api_token "
                        "FROM users WHERE api_token = %s AND is_active = true;",
                        (token,),
                    )
                    row = cur.fetchone()
            return cls._row_to_user(row) if row else None
        except Exception:
            return None

    @classmethod
    def create(cls, username: str, password: str, email: str | None = None) -> "User | None":
        pw_hash = generate_password_hash(password)
        try:
            with psycopg.connect(get_config().database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO users (username, email, password_hash)
                        VALUES (%s, %s, %s)
                        RETURNING id, username, email, is_admin, is_active, api_token;
                        """,
                        (username, email or None, pw_hash),
                    )
                    row = cur.fetchone()
                conn.commit()
            return cls._row_to_user(row) if row else None
        except Exception:
            return None

    def check_password(self, password: str) -> bool:
        try:
            with psycopg.connect(get_config().database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT password_hash FROM users WHERE id = %s;",
                        (self.id,),
                    )
                    row = cur.fetchone()
            if not row or not row[0]:
                return False
            return check_password_hash(row[0], password)
        except Exception:
            return False

    def rotate_token(self) -> str:
        try:
            with psycopg.connect(get_config().database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET api_token = gen_random_uuid()::text "
                        "WHERE id = %s RETURNING api_token;",
                        (self.id,),
                    )
                    row = cur.fetchone()
                conn.commit()
            if row:
                self.api_token = row[0]
            return self.api_token
        except Exception:
            return self.api_token


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    return User.get_by_id(user_id)
