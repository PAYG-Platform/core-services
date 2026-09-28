# backend/config-service/app/database.py
import os
import sys
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    logging.critical("DATABASE_URL environment variable is missing! Check your .env or docker-compose file.")
    sys.exit(1)

# Ensure psycopg2 driver is explicitly used for postgresql:// URLs
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency that yields a SQLAlchemy database session for request lifecycle."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()