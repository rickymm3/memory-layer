web: python db/apply_migrations.py && gunicorn 'app_main:create_app()' --bind 0.0.0.0:$PORT --workers 2 --timeout 60
