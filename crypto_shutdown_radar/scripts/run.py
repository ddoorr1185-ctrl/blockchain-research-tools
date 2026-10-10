#!/usr/bin/env python3
"""Starts the background scheduler in this process (blocking). Run the
dashboard separately with:
    uvicorn app.dashboard.main:app --port 8000
"""
from app.db import init_db
from app.scheduler import run_forever

if __name__ == "__main__":
    init_db()
    run_forever()
