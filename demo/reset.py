"""Empty the demo locally (same code as the nightly POST /demo/reset).

    docker compose run --rm -T api uv run --frozen python -m demo.reset
"""

from app.config import get_settings
from app.db.session import SessionLocal
from app.demo.reset import reset_demo

if __name__ == "__main__":
    with SessionLocal() as db:
        print(reset_demo(db, get_settings()))
