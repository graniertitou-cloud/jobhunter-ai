"""ICART Stage Finder - Backend API.

A FastAPI application for managing internship/apprenticeship searches
for ICART art and culture school students.
"""

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import csv
import io
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Generator, Optional
from urllib.parse import quote, urlparse

# ---------------------------------------------------------------------------
# Third-party
# ---------------------------------------------------------------------------
import bcrypt
import pdfplumber
import requests as http_requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    from openai import OpenAI

    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_EMAIL_LENGTH = 200
MAX_PASSWORD_LENGTH = 200
MIN_PASSWORD_LENGTH = 8
MAX_NAME_LENGTH = 100
MAX_KEYWORD_LENGTH = 200
MAX_URL_LENGTH = 2000
MAX_DESCRIPTION_LENGTH = 10_000
MAX_NOTES_LENGTH = 2000
MAX_MESSAGE_LENGTH = 2000
MAX_CV_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_SCRAPE_RESULTS_PER_PLATFORM = 15
MAX_DESCRIPTION_FETCH_JOBS = 10
MAX_DESCRIPTION_CHARS = 3000
MAX_DESCRIPTION_PREVIEW = 500
MAX_ADMIN_STUDENTS = 500
MAX_STUDENT_APPLICATIONS = 100
SCRAPER_THREAD_COUNT = 5
SCRAPER_TIMEOUT_SECONDS = 30
SCRAPER_FUTURE_TIMEOUT = 15
SESSION_DURATION_HOURS = 24
GROQ_BATCH_SIZE = 8
GROQ_CV_PREVIEW_CHARS = 1500
GROQ_DESCRIPTION_PREVIEW = 400
HTTP_REQUEST_TIMEOUT = 12
RETRY_DELAY_SECONDS = 5
PDF_MAGIC_BYTES = b"%PDF-"

VALID_STAGE_STATUSES = {"searching", "found"}
VALID_APPLICATION_STATUSES = {
    "to_send", "sent", "followed_up", "interview", "rejected", "accepted",
}
STATUS_MAP = {
    "a_envoyer": "to_send",
    "envoye": "sent",
    "relance": "followed_up",
    "entretien": "interview",
    "refuse": "rejected",
    "accepte": "accepted",
    "to_send": "to_send",
    "sent": "sent",
    "followed_up": "followed_up",
    "interview": "interview",
    "rejected": "rejected",
    "accepted": "accepted",
}
BLOCKED_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "metadata.google.internal",
    "169.254.169.254",
})
DEFAULT_SCHOOLS = ("ICART Paris", "ICART Bordeaux", "ICART Lyon")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("icart")

# ---------------------------------------------------------------------------
# Groq client (optional)
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
groq_client: Optional[Any] = None
if GROQ_API_KEY and HAS_OPENAI:
    groq_client = OpenAI(
        api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1"
    )

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///icart.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Yield a database session with automatic cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Ensure directories
# ---------------------------------------------------------------------------
os.makedirs("static", exist_ok=True)
os.makedirs("uploads", exist_ok=True)


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(MAX_EMAIL_LENGTH), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(20), default="student")
    session_token = Column(String(64), default="", index=True)
    session_expires_at = Column(DateTime, nullable=True)
    first_name = Column(String(MAX_NAME_LENGTH), default="")
    last_name = Column(String(MAX_NAME_LENGTH), default="")


class School(Base):
    __tablename__ = "schools"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    active = Column(Boolean, default=True)


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    first_name = Column(String(MAX_NAME_LENGTH), default="")
    last_name = Column(String(MAX_NAME_LENGTH), default="")
    promo = Column(String(100), default="")
    school_id = Column(Integer, nullable=True, index=True)
    target_sector = Column(String(100), default="")
    cv_text = Column(Text, default="")
    cv_filename = Column(String(256), default="")
    stage_status = Column(String(20), default="searching")
    stage_company = Column(String(200), default="")
    domain_found = Column(String(200), default="")
    last_activity_at = Column(DateTime, nullable=True)
    notes = Column(Text, default="")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False)
    company = Column(String(200), default="")
    location = Column(String(200), default="")
    url = Column(String(MAX_URL_LENGTH), default="", index=True)
    platform = Column(String(100), default="")
    sector = Column(String(100), default="")
    description = Column(Text, default="")
    contract_type = Column(String(50), default="")
    score = Column(Float, default=0)
    explanation = Column(Text, default="")
    scraped_at = Column(DateTime, default=datetime.utcnow)


class SavedJob(Base):
    __tablename__ = "saved_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, nullable=False, index=True)
    job_id = Column(Integer, nullable=False)
    saved_at = Column(DateTime, default=datetime.utcnow)


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, nullable=False, index=True)
    job_title = Column(String(200), default="")
    company = Column(String(200), default="")
    url = Column(String(500), default="")
    status = Column(String(50), default="to_send")
    notes = Column(Text, default="")
    applied_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_user_id = Column(Integer, nullable=False)
    to_student_id = Column(Integer, nullable=False, index=True)
    content = Column(Text, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow)
    read = Column(Integer, default=0)


# ---------------------------------------------------------------------------
# Create tables + migrations
# ---------------------------------------------------------------------------
Base.metadata.create_all(bind=engine)


def _migrate() -> None:
    """Add missing columns to existing tables for schema evolution."""
    import sqlalchemy as sa

    insp = sa.inspect(engine)
    migrations = [
        ("users", "first_name", "VARCHAR DEFAULT ''"),
        ("users", "last_name", "VARCHAR DEFAULT ''"),
        ("students", "promo", "VARCHAR DEFAULT ''"),
        ("students", "school_id", "INTEGER"),
        ("students", "cv_text", "TEXT DEFAULT ''"),
        ("students", "cv_filename", "VARCHAR DEFAULT ''"),
        ("students", "domain_found", "VARCHAR DEFAULT ''"),
        ("students", "notes", "TEXT DEFAULT ''"),
        ("jobs", "contract_type", "VARCHAR DEFAULT ''"),
    ]
    table_names = insp.get_table_names()
    for table, col, col_type in migrations:
        if table not in table_names:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table)}
        if col in existing_cols:
            continue
        try:
            with engine.connect() as conn:
                conn.execute(
                    sa.text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                )
                conn.commit()
            logger.info("Migrated column: %s.%s", table, col)
        except OperationalError:
            logger.warning("Migration skipped (already exists): %s.%s", table, col)


_migrate()


# ---------------------------------------------------------------------------
# Seed default data
# ---------------------------------------------------------------------------
def _create_defaults() -> None:
    """Create default admin user and schools if they do not exist."""
    with get_db() as db:
        if not db.query(User).filter(User.email == "admin@icart.fr").first():
            hashed = bcrypt.hashpw(
                "icart2025admin".encode(), bcrypt.gensalt()
            ).decode()
            db.add(
                User(
                    email="admin@icart.fr",
                    password_hash=hashed,
                    role="admin",
                    first_name="Admin",
                    last_name="ICART",
                )
            )
            db.commit()
            logger.info("Default admin created.")

        for school_name in DEFAULT_SCHOOLS:
            if not db.query(School).filter(School.name == school_name).first():
                db.add(School(name=school_name))
        db.commit()
        logger.info("Default schools ensured.")


_create_defaults()


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=MAX_EMAIL_LENGTH)
    password: str = Field(..., max_length=MAX_PASSWORD_LENGTH)
    first_name: str = Field("", max_length=MAX_NAME_LENGTH)
    last_name: str = Field("", max_length=MAX_NAME_LENGTH)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", v):
            raise ValueError("Format d'email invalide")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} caracteres"
            )
        return v


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=MAX_EMAIL_LENGTH)
    password: str = Field(..., max_length=MAX_PASSWORD_LENGTH)
    expected_role: str = Field("", max_length=20)


class ScrapeRequest(BaseModel):
    keywords: str = Field(..., max_length=MAX_KEYWORD_LENGTH)
    sector: str = Field("", max_length=100)
    location: str = Field("Paris", max_length=100)


class SchoolCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class SchoolUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class StudentProfileUpdate(BaseModel):
    first_name: str = Field("", max_length=MAX_NAME_LENGTH)
    last_name: str = Field("", max_length=MAX_NAME_LENGTH)
    school_id: Optional[int] = None
    promo: str = Field("", max_length=100)
    target_sector: str = Field("", max_length=100)


class SaveJobRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    company: str = Field("", max_length=200)
    location: str = Field("", max_length=200)
    url: str = Field("", max_length=MAX_URL_LENGTH)
    platform: str = Field("", max_length=100)
    description: str = Field("", max_length=MAX_DESCRIPTION_LENGTH)
    contract_type: str = Field("", max_length=50)
    sector: str = Field("", max_length=100)
    score: float = 0
    explanation: str = Field("", max_length=5000)


class ApplicationCreate(BaseModel):
    job_title: str = Field("", max_length=200)
    title: str = Field("", max_length=200)
    company: str = Field("", max_length=200)
    url: str = Field("", max_length=500)
    notes: str = Field("", max_length=MAX_NOTES_LENGTH)
    status: str = Field("", max_length=50)


class ApplicationUpdate(BaseModel):
    status: str = Field(..., max_length=50)
    notes: str = Field("", max_length=MAX_NOTES_LENGTH)


class StatusUpdate(BaseModel):
    stage_status: str = Field(..., max_length=50)
    stage_company: str = Field("", max_length=200)

    @field_validator("stage_status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in VALID_STAGE_STATUSES:
            raise ValueError("Statut invalide (searching ou found)")
        return v


class AdminStudentUpdate(BaseModel):
    domain_found: Optional[str] = Field(None, max_length=200)
    stage_status: Optional[str] = Field(None, max_length=50)
    stage_company: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=MAX_NOTES_LENGTH)
    promo: Optional[str] = Field(None, max_length=100)
    school_id: Optional[int] = None
    target_sector: Optional[str] = Field(None, max_length=100)


class StudentMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


class SendMessageRequest(BaseModel):
    to_student_id: int
    content: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def validate_url(url: str) -> bool:
    """Validate a URL for safety (SSRF protection)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname or ""
        if hostname in BLOCKED_HOSTS:
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def _serialize_user(user: User) -> dict[str, Any]:
    """Serialize a User to a safe dict (no password hash)."""
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


def _serialize_student_profile(
    student: Student, email: str, school_name: Optional[str]
) -> dict[str, Any]:
    """Serialize a Student profile to a dict."""
    return {
        "id": student.id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "email": email,
        "school_id": student.school_id,
        "school_name": school_name,
        "promo": student.promo,
        "target_sector": student.target_sector,
        "stage_status": student.stage_status,
        "stage_company": student.stage_company,
        "domain_found": student.domain_found,
        "cv_filename": student.cv_filename,
        "has_cv": bool(student.cv_filename),
    }


def _serialize_application(app_obj: Application) -> dict[str, Any]:
    """Serialize an Application to a dict."""
    return {
        "id": app_obj.id,
        "job_title": app_obj.job_title,
        "company": app_obj.company,
        "url": app_obj.url,
        "status": app_obj.status,
        "notes": app_obj.notes,
        "applied_at": app_obj.applied_at.isoformat() if app_obj.applied_at else None,
        "updated_at": app_obj.updated_at.isoformat() if app_obj.updated_at else None,
    }


def _serialize_message(msg: Message, viewer_user_id: int) -> dict[str, Any]:
    """Serialize a Message to a dict."""
    return {
        "id": msg.id,
        "from_user_id": msg.from_user_id,
        "to_student_id": msg.to_student_id,
        "content": msg.content,
        "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
        "read": msg.read,
        "from_admin": msg.from_user_id != viewer_user_id,
    }


def _get_school_name(db: Session, school_id: Optional[int]) -> Optional[str]:
    """Look up a school name by ID, returning None if not found."""
    if not school_id:
        return None
    school = db.query(School).filter(School.id == school_id).first()
    return school.name if school else None


def _get_or_create_student(db: Session, user_id: int) -> Student:
    """Return the Student for a user, creating one if absent."""
    student = db.query(Student).filter(Student.user_id == user_id).first()
    if not student:
        student = Student(user_id=user_id, last_activity_at=datetime.utcnow())
        db.add(student)
        db.commit()
        db.refresh(student)
    return student


def _resolve_application_status(raw: str) -> str:
    """Map a frontend or backend status string to a canonical backend status."""
    return STATUS_MAP.get(raw, "to_send") if raw else "to_send"


def _safe_filepath(filename: str) -> str:
    """Build a safe path under the uploads directory and verify it stays there."""
    uploads_dir = os.path.abspath("uploads")
    filepath = os.path.abspath(os.path.join("uploads", filename))
    if not filepath.startswith(uploads_dir):
        raise HTTPException(400, "Nom de fichier invalide")
    return filepath


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def get_current_user(request: Request) -> Optional[User]:
    """Extract and validate the current user from the Authorization header."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token or len(token) > 128:
        return None
    with get_db() as db:
        user = db.query(User).filter(User.session_token == token).first()
        if not user:
            return None
        if user.session_expires_at and user.session_expires_at < datetime.utcnow():
            return None
        db.expunge(user)
        return user


def require_user(request: Request) -> User:
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Non authentifie")
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if user.role != "admin":
        raise HTTPException(403, "Acces refuse")
    return user


def require_student(request: Request) -> User:
    user = require_user(request)
    if user.role != "student":
        raise HTTPException(403, "Acces refuse")
    return user


# ---------------------------------------------------------------------------
# Security middleware
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
DEBUG = os.getenv("DEBUG", "").lower() in ("true", "1", "yes")

app = FastAPI(
    title="ICART Stage Finder",
    docs_url="/docs" if DEBUG else None,
    redoc_url=None,
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(SecurityHeadersMiddleware)

origins = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:8000,http://localhost:8001"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500, content={"detail": "Erreur interne du serveur."}
    )


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

_REFERERS = [
    "https://www.google.com/",
    "https://www.google.fr/",
    "https://www.bing.com/",
]

_ACCEPT_LANGUAGES = [
    "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "fr-FR,fr;q=0.9,en;q=0.8",
    "en-US,en;q=0.9,fr-FR;q=0.8,fr;q=0.7",
]

_DESCRIPTION_SELECTORS = [
    "div.description",
    "div.job-description",
    "section.description",
    "div[class*='description']",
    "div[class*='Description']",
    "article",
    "div.content",
    "main",
]


def _random_headers() -> dict[str, str]:
    """Return headers with a random User-Agent and realistic browser fields."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": random.choice(_REFERERS),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


def safe_request(
    url: str, method: str = "get", **kwargs: Any
) -> Optional[http_requests.Response]:
    """Make an HTTP request with random headers, retry once on 429/403."""
    if not validate_url(url):
        return None
    kwargs.setdefault("headers", _random_headers())
    kwargs.setdefault("timeout", HTTP_REQUEST_TIMEOUT)
    kwargs.setdefault("allow_redirects", True)
    try:
        resp = getattr(http_requests, method)(url, **kwargs)
        if resp.status_code in (429, 403):
            logger.info("Got %d, retrying after delay", resp.status_code)
            time.sleep(RETRY_DELAY_SECONDS)
            kwargs["headers"] = _random_headers()
            resp = getattr(http_requests, method)(url, **kwargs)
        return resp
    except (http_requests.RequestException, OSError):
        logger.warning("HTTP request failed for URL")
        return None


def detect_contract_type(text: str) -> str:
    """Detect contract type from job text. Returns 'Stage', 'Alternance', or ''."""
    upper = text.upper()
    if "ALTERNANCE" in upper or "APPRENTISSAGE" in upper:
        return "Alternance"
    if "STAGE" in upper or "STAGIAIRE" in upper or "INTERN" in upper:
        return "Stage"
    return ""


def fetch_job_description(url: str) -> str:
    """Fetch and extract the job description text from a URL."""
    if not url or url == "#" or not validate_url(url):
        return ""
    try:
        resp = safe_request(url, timeout=10)
        if resp is None or resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.content, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        for selector in _DESCRIPTION_SELECTORS:
            el = soup.select_one(selector)
            if el and len(el.get_text(strip=True)) > 100:
                return el.get_text(separator="\n", strip=True)[:MAX_DESCRIPTION_CHARS]
        body = soup.find("body")
        if body:
            return body.get_text(separator="\n", strip=True)[:MAX_DESCRIPTION_CHARS]
        return ""
    except Exception:
        logger.warning("Failed to fetch job description")
        return ""


def scrape_linkedin(keywords: str, location: str) -> list[dict[str, str]]:
    """Scrape LinkedIn public job listings."""
    jobs: list[dict[str, str]] = []
    try:
        url = (
            f"https://www.linkedin.com/jobs/search?"
            f"keywords={quote(keywords)}&location={quote(location)}&f_TPR=r604800"
        )
        resp = safe_request(url)
        if resp is None or resp.status_code != 200:
            return jobs
        soup = BeautifulSoup(resp.content, "lxml")
        for card in soup.find_all("div", class_="base-card")[
            :MAX_SCRAPE_RESULTS_PER_PLATFORM
        ]:
            title_el = card.find("h3", class_="base-search-card__title")
            company_el = card.find("h4", class_="base-search-card__subtitle")
            loc_el = card.find("span", class_="job-search-card__location")
            link_el = card.find("a", class_="base-card__full-link")
            title = title_el.get_text().strip() if title_el else ""
            company = company_el.get_text().strip() if company_el else ""
            if not title or not company:
                continue
            contract = detect_contract_type(title + " " + card.get_text())
            if contract not in ("Stage", "Alternance"):
                continue
            jobs.append({
                "title": title,
                "company": company,
                "location": loc_el.get_text().strip() if loc_el else location,
                "url": link_el["href"] if link_el else "",
                "platform": "LinkedIn",
                "contract_type": contract,
                "description": "",
            })
        time.sleep(random.uniform(1, 3))
    except Exception:
        logger.warning("LinkedIn scraping error")
    return jobs


def scrape_france_travail(keywords: str, location: str) -> list[dict[str, str]]:
    """Scrape France Travail (ex-Pole Emploi)."""
    jobs: list[dict[str, str]] = []
    try:
        url = (
            f"https://candidat.francetravail.fr/offres/recherche?"
            f"motsCles={quote(keywords)}&offresPartenaires=true"
        )
        resp = safe_request(url)
        if resp is None or resp.status_code != 200:
            return jobs
        soup = BeautifulSoup(resp.content, "lxml")
        for li in soup.select("li.result")[:20]:
            title_el = li.find("h2")
            title = ""
            if title_el:
                span = title_el.find("span", class_="media-heading-title")
                title = (
                    span.get_text().strip()
                    if span
                    else title_el.get_text().strip()
                )
            if not title:
                continue
            subtext = li.find("p", class_="subtext")
            company, loc = "", location
            if subtext:
                parts = subtext.get_text().strip().split(" - ")
                company = parts[0].strip() if parts else ""
                if len(parts) > 1:
                    loc = parts[-1].strip()
            link_el = li.find("a", href=True)
            link = ""
            if link_el:
                href = link_el["href"]
                link = (
                    f"https://candidat.francetravail.fr{href}"
                    if href.startswith("/")
                    else href
                )
            contract = detect_contract_type(li.get_text())
            if contract not in ("Stage", "Alternance"):
                continue
            jobs.append({
                "title": title,
                "company": company,
                "location": loc,
                "url": link,
                "platform": "France Travail",
                "contract_type": contract,
                "description": "",
            })
        time.sleep(random.uniform(0.5, 1))
    except Exception:
        logger.warning("France Travail scraping error")
    return jobs


def scrape_with_ddgs(
    query: str, platform_name: str, max_results: int = 10
) -> list[dict[str, str]]:
    """Use DuckDuckGo search to find job listings for a platform."""
    jobs: list[dict[str, str]] = []
    try:
        results = DDGS().text(query, max_results=max_results)
        for r in results:
            title = r.get("title", "")
            url = r.get("href", "")
            body = r.get("body", "")
            company = ""
            for sep in (" - ", " | "):
                if sep in title:
                    parts = title.rsplit(sep, 1)
                    if len(parts) == 2:
                        title, company = parts[0].strip(), parts[1].strip()
                    break
            contract = detect_contract_type(title + " " + body) or "Stage"
            jobs.append({
                "title": title,
                "company": company,
                "location": "",
                "url": url,
                "platform": platform_name,
                "contract_type": contract,
                "description": body,
            })
    except Exception:
        logger.warning("DDGS scrape error for platform: %s", platform_name)
    return jobs


def deduplicate(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate jobs by (title, company) tuple."""
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for j in jobs:
        key = (j["title"].lower().strip(), j["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(j)
    return unique


def score_jobs_with_groq(
    jobs: list[dict], cv_text: str, target_sector: str
) -> list[dict]:
    """Score jobs using Groq LLM based on student CV and target sector."""
    if not groq_client or not jobs:
        for j in jobs:
            j["score"] = round(random.uniform(4, 8), 1)
            j["explanation"] = "Scoring IA non disponible."
        return jobs

    if not cv_text and not target_sector:
        for j in jobs:
            j["score"] = round(random.uniform(4, 8), 1)
            j["explanation"] = "Completez votre profil pour un scoring personnalise."
        return jobs

    batches = [
        jobs[i : i + GROQ_BATCH_SIZE]
        for i in range(0, len(jobs), GROQ_BATCH_SIZE)
    ]
    scored: list[dict] = []
    for batch in batches:
        job_lines = []
        for j in batch:
            line = f"- {j['title']} chez {j['company']} ({j['contract_type']})"
            if j.get("description"):
                line += f"\n  Description: {j['description'][:GROQ_DESCRIPTION_PREVIEW]}"
            job_lines.append(line)

        prompt = (
            "Tu es un conseiller en stage/alternance pour etudiants en ecole d'art "
            "et culture (ICART).\n"
            f"Profil etudiant:\nCV: {cv_text[:GROQ_CV_PREVIEW_CHARS]}\n"
            f"Secteur vise: {target_sector}\n\n"
            f"Offres:\n" + "\n".join(job_lines) + "\n\n"
            "Pour chaque offre, donne un score de 1 a 10 (10 = correspond "
            "parfaitement) et UNE phrase d'explication en francais.\n"
            "Reponds UNIQUEMENT en JSON valide, un tableau d'objets avec "
            '"score" (number) et "explanation" (string).'
        )
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
            )
            content = resp.choices[0].message.content.strip()
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                results = json.loads(content[start:end])
                for i, j in enumerate(batch):
                    if i < len(results):
                        j["score"] = min(10, max(1, results[i].get("score", 5)))
                        j["explanation"] = results[i].get("explanation", "")
                    else:
                        j["score"] = 5
                        j["explanation"] = ""
            else:
                for j in batch:
                    j["score"] = 5
                    j["explanation"] = ""
        except Exception:
            logger.warning("Groq scoring error")
            for j in batch:
                j["score"] = round(random.uniform(4, 8), 1)
                j["explanation"] = "Scoring IA temporairement indisponible."
        scored.extend(batch)
    return scored


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/auth/register")
@limiter.limit("10/minute")
def register(req: RegisterRequest, request: Request) -> dict[str, Any]:
    with get_db() as db:
        if db.query(User).filter(User.email == req.email).first():
            raise HTTPException(400, "Cet email est deja utilise")

        user = User(
            email=req.email,
            password_hash=hash_password(req.password),
            role="student",
            first_name=req.first_name,
            last_name=req.last_name,
            session_token=secrets.token_urlsafe(32),
            session_expires_at=datetime.utcnow()
            + timedelta(hours=SESSION_DURATION_HOURS),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        student = Student(
            user_id=user.id,
            first_name=req.first_name,
            last_name=req.last_name,
            last_activity_at=datetime.utcnow(),
        )
        db.add(student)
        db.commit()

        return {"token": user.session_token, "user": _serialize_user(user)}


@app.post("/api/auth/login")
@limiter.limit("5/minute")
def login(req: LoginRequest, request: Request) -> dict[str, Any]:
    dummy_hash = hash_password("dummy_password_for_timing")

    with get_db() as db:
        user = db.query(User).filter(User.email == req.email.strip().lower()).first()
        if not user:
            verify_password(req.password, dummy_hash)
            raise HTTPException(401, "Email ou mot de passe incorrect")

        if not verify_password(req.password, user.password_hash):
            raise HTTPException(401, "Email ou mot de passe incorrect")

        if req.expected_role and user.role != req.expected_role:
            if req.expected_role == "admin":
                raise HTTPException(
                    403, "Ce compte n'est pas un compte administrateur"
                )
            raise HTTPException(403, "Ce compte n'est pas un compte etudiant")

        user.session_token = secrets.token_urlsafe(32)
        user.session_expires_at = datetime.utcnow() + timedelta(
            hours=SESSION_DURATION_HOURS
        )
        db.commit()

        return {"token": user.session_token, "user": _serialize_user(user)}


@app.post("/api/auth/logout")
def logout(request: Request) -> dict[str, str]:
    user = require_user(request)
    with get_db() as db:
        u = db.query(User).filter(User.id == user.id).first()
        if u:
            u.session_token = ""
            u.session_expires_at = None
            db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "ICART Stage Finder"}


# ---------------------------------------------------------------------------
# School endpoints
# ---------------------------------------------------------------------------
@app.get("/api/schools")
def list_schools() -> list[dict[str, Any]]:
    with get_db() as db:
        schools = (
            db.query(School)
            .filter(School.active == True)  # noqa: E712
            .order_by(School.name)
            .all()
        )
        return [{"id": s.id, "name": s.name} for s in schools]


@app.post("/api/admin/schools")
@limiter.limit("20/minute")
def create_school(req: SchoolCreate, request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        school = School(name=req.name)
        db.add(school)
        db.commit()
        db.refresh(school)
        return {"id": school.id, "name": school.name}


@app.patch("/api/admin/schools/{school_id}")
@limiter.limit("20/minute")
def update_school(
    school_id: int, req: SchoolUpdate, request: Request
) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        school = db.query(School).filter(School.id == school_id).first()
        if not school:
            raise HTTPException(404, "Ecole non trouvee")
        school.name = req.name
        db.commit()
        db.refresh(school)
        return {"id": school.id, "name": school.name}


# ---------------------------------------------------------------------------
# Student profile endpoints
# ---------------------------------------------------------------------------
@app.get("/api/student/profile")
def student_profile(request: Request) -> dict[str, Any]:
    user = require_student(request)
    with get_db() as db:
        student = _get_or_create_student(db, user.id)
        student.last_activity_at = datetime.utcnow()
        db.commit()
        db.refresh(student)
        school_name = _get_school_name(db, student.school_id)
        return _serialize_student_profile(student, user.email, school_name)


@app.post("/api/student/profile")
@limiter.limit("20/minute")
def update_student_profile(
    req: StudentProfileUpdate, request: Request
) -> dict[str, Any]:
    user = require_student(request)
    with get_db() as db:
        student = _get_or_create_student(db, user.id)
        student.first_name = req.first_name
        student.last_name = req.last_name
        student.school_id = req.school_id
        student.promo = req.promo
        student.target_sector = req.target_sector
        student.last_activity_at = datetime.utcnow()
        db.commit()
        db.refresh(student)
        school_name = _get_school_name(db, student.school_id)
        return _serialize_student_profile(student, user.email, school_name)


@app.post("/api/student/upload-cv")
@limiter.limit("10/minute")
async def upload_cv(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    user = require_student(request)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Seuls les fichiers PDF sont acceptes")

    content = await file.read()

    if len(content) > MAX_CV_SIZE_BYTES:
        raise HTTPException(400, "Le fichier ne doit pas depasser 10 Mo")

    if not content[:5] == PDF_MAGIC_BYTES:
        raise HTTPException(400, "Le fichier n'est pas un PDF valide")

    safe_filename = f"cv_{user.id}_{int(datetime.utcnow().timestamp())}.pdf"
    filepath = _safe_filepath(safe_filename)
    with open(filepath, "wb") as f:
        f.write(content)

    cv_text = ""
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    cv_text += page_text + "\n"
    except Exception:
        logger.warning("PDF text extraction error")

    with get_db() as db:
        student = _get_or_create_student(db, user.id)

        if student.cv_filename:
            try:
                old_path = _safe_filepath(student.cv_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)
            except Exception:
                pass

        student.cv_filename = safe_filename
        student.cv_text = cv_text.strip()
        student.last_activity_at = datetime.utcnow()
        db.commit()

    return {
        "status": "ok",
        "cv_filename": safe_filename,
        "text_length": len(cv_text),
    }


@app.get("/api/student/cv")
def serve_student_cv(request: Request) -> FileResponse:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student or not student.cv_filename:
            raise HTTPException(404, "Aucun CV trouve")
        filepath = _safe_filepath(student.cv_filename)
        if not os.path.exists(filepath):
            raise HTTPException(404, "Fichier CV introuvable")
        return FileResponse(
            filepath, media_type="application/pdf", filename=student.cv_filename
        )


# ---------------------------------------------------------------------------
# Shared scraping logic
# ---------------------------------------------------------------------------
def _run_scrape(keywords: str, sector: str, location: str) -> list[dict]:
    """Run the parallel scraping pipeline. Returns deduplicated job list."""
    stage_query = f"stage {keywords} {sector}"
    alt_query = f"alternance {keywords} {sector}"

    tasks = [
        lambda q=stage_query, l=location: scrape_linkedin(q, l),
        lambda q=alt_query, l=location: scrape_linkedin(q, l),
        lambda q=stage_query, l=location: scrape_france_travail(q, l),
        lambda q=f"{stage_query} {location} site:welcometothejungle.com": scrape_with_ddgs(
            q, "WTTJ", 8
        ),
        lambda q=f"stage {keywords} {sector} site:profilculture.com": scrape_with_ddgs(
            q, "Profilculture", 8
        ),
    ]

    all_jobs: list[dict] = []
    with ThreadPoolExecutor(max_workers=SCRAPER_THREAD_COUNT) as executor:
        futures = {executor.submit(fn): idx for idx, fn in enumerate(tasks)}
        for future in as_completed(futures, timeout=SCRAPER_TIMEOUT_SECONDS):
            try:
                result = future.result(timeout=SCRAPER_FUTURE_TIMEOUT)
                if result:
                    all_jobs.extend(result)
            except Exception:
                logger.warning("Scrape task failed")

    all_jobs = deduplicate(all_jobs)

    if not all_jobs:
        return []

    # Fetch descriptions in parallel for jobs missing one
    jobs_needing_desc = [
        j
        for j in all_jobs[:MAX_DESCRIPTION_FETCH_JOBS]
        if not j.get("description") and j.get("url")
    ]
    if jobs_needing_desc:

        def _fetch_desc(job: dict) -> dict:
            try:
                desc = fetch_job_description(job["url"])
                if desc:
                    job["description"] = desc
                    if not job.get("contract_type"):
                        job["contract_type"] = detect_contract_type(desc)
            except Exception:
                pass
            return job

        with ThreadPoolExecutor(max_workers=SCRAPER_THREAD_COUNT) as executor:
            list(executor.map(_fetch_desc, jobs_needing_desc, timeout=20))

    return all_jobs


def _format_jobs(jobs: list[dict]) -> list[dict]:
    """Format job list for API response."""
    return [
        {
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "location": j.get("location", ""),
            "url": j.get("url", ""),
            "platform": j.get("platform", ""),
            "description": (j.get("description", "") or "")[
                :MAX_DESCRIPTION_PREVIEW
            ],
            "contract_type": j.get("contract_type", ""),
            "score": j.get("score", 0),
            "explanation": j.get("explanation", ""),
        }
        for j in jobs
    ]


# ---------------------------------------------------------------------------
# Student search (main scraping endpoint)
# ---------------------------------------------------------------------------
@app.post("/api/student/search")
@limiter.limit("5/minute")
def student_search(req: ScrapeRequest, request: Request) -> dict[str, Any]:
    user = require_student(request)
    keywords = req.keywords.strip()
    sector = req.sector.strip()
    location = req.location.strip() or "Paris"

    if not keywords:
        raise HTTPException(400, "Mots-cles requis")

    all_jobs = _run_scrape(keywords, sector, location)

    if not all_jobs:
        return {
            "jobs": [],
            "count": 0,
            "message": "Aucune offre trouvee. Essayez d'autres mots-cles.",
        }

    # Score with Groq
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        cv_text = student.cv_text if student else ""
        target_sector = student.target_sector if student else sector
        if student:
            student.last_activity_at = datetime.utcnow()
            db.commit()

    all_jobs = score_jobs_with_groq(all_jobs, cv_text, target_sector)
    all_jobs.sort(key=lambda x: x.get("score", 0), reverse=True)

    return {"jobs": _format_jobs(all_jobs), "count": len(all_jobs)}


# ---------------------------------------------------------------------------
# Student saved jobs
# ---------------------------------------------------------------------------
@app.post("/api/student/save-job")
@limiter.limit("30/minute")
def save_job(req: SaveJobRequest, request: Request) -> dict[str, Any]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            raise HTTPException(404, "Profil etudiant introuvable")

        job = None
        if req.url:
            job = db.query(Job).filter(Job.url == req.url).first()
        if not job:
            job = db.query(Job).filter(
                func.lower(Job.title) == req.title.lower(),
                func.lower(Job.company) == req.company.lower(),
            ).first()

        if not job:
            job = Job(
                title=req.title,
                company=req.company,
                location=req.location,
                url=req.url,
                platform=req.platform,
                sector=req.sector,
                description=req.description,
                contract_type=req.contract_type,
                score=req.score,
                explanation=req.explanation,
                scraped_at=datetime.utcnow(),
            )
            db.add(job)
            db.commit()
            db.refresh(job)

        existing = db.query(SavedJob).filter(
            SavedJob.student_id == student.id, SavedJob.job_id == job.id
        ).first()
        if existing:
            raise HTTPException(400, "Offre deja sauvegardee")

        saved = SavedJob(
            student_id=student.id, job_id=job.id, saved_at=datetime.utcnow()
        )
        db.add(saved)
        student.last_activity_at = datetime.utcnow()
        db.commit()
        db.refresh(saved)
        return {"id": saved.id, "job_id": job.id, "status": "saved"}


@app.get("/api/student/saved-jobs")
def list_saved_jobs(request: Request) -> list[dict[str, Any]]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            return []
        saved_jobs = (
            db.query(SavedJob)
            .filter(SavedJob.student_id == student.id)
            .order_by(SavedJob.saved_at.desc())
            .all()
        )
        job_ids = [s.job_id for s in saved_jobs]
        if not job_ids:
            return []
        jobs_by_id = {
            j.id: j for j in db.query(Job).filter(Job.id.in_(job_ids)).all()
        }
        result = []
        for s in saved_jobs:
            job = jobs_by_id.get(s.job_id)
            if not job:
                continue
            result.append({
                "id": s.id,
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "url": job.url,
                "platform": job.platform,
                "contract_type": job.contract_type,
                "score": job.score,
                "explanation": job.explanation,
                "saved_at": s.saved_at.isoformat() if s.saved_at else None,
            })
        return result


@app.delete("/api/student/saved-jobs/{saved_id}")
def delete_saved_job(saved_id: int, request: Request) -> dict[str, str]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            raise HTTPException(404, "Profil etudiant introuvable")
        saved = db.query(SavedJob).filter(SavedJob.id == saved_id).first()
        if not saved:
            raise HTTPException(404, "Sauvegarde introuvable")
        if saved.student_id != student.id:
            raise HTTPException(403, "Acces refuse")
        db.delete(saved)
        db.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Student applications
# ---------------------------------------------------------------------------
@app.get("/api/student/stats")
def student_stats(request: Request) -> dict[str, Any]:
    """Return detailed stats for the student dashboard."""
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            return {"total_apps": 0, "by_status": {}, "saved_count": 0, "weekly": []}

        apps = (
            db.query(Application)
            .filter(Application.student_id == student.id)
            .all()
        )
        saved_count = (
            db.query(SavedJob)
            .filter(SavedJob.student_id == student.id)
            .count()
        )

        # By status
        status_counts = {}
        for a in apps:
            s = a.status or "to_send"
            status_counts[s] = status_counts.get(s, 0) + 1

        # Weekly activity (last 8 weeks)
        weekly = []
        now = datetime.utcnow()
        for i in range(7, -1, -1):
            week_start = now - timedelta(weeks=i + 1)
            week_end = now - timedelta(weeks=i)
            count = sum(
                1 for a in apps
                if a.applied_at and week_start <= a.applied_at < week_end
            )
            label = week_start.strftime("%d/%m")
            weekly.append({"week": label, "count": count})

        # Response rate
        total = len(apps)
        responded = sum(
            1 for a in apps
            if a.status in ("entretien", "obtenu", "refusee")
        )
        response_rate = round((responded / total) * 100) if total > 0 else 0

        return {
            "total_apps": total,
            "by_status": status_counts,
            "saved_count": saved_count,
            "weekly": weekly,
            "response_rate": response_rate,
            "has_cv": bool(student.cv_filename),
            "stage_status": student.stage_status,
        }


@app.get("/api/student/applications")
def student_applications(request: Request) -> list[dict[str, Any]]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            return []
        apps = (
            db.query(Application)
            .filter(Application.student_id == student.id)
            .order_by(Application.updated_at.desc())
            .limit(MAX_STUDENT_APPLICATIONS)
            .all()
        )
        return [_serialize_application(a) for a in apps]


@app.post("/api/student/applications")
@limiter.limit("30/minute")
def create_application(
    req: ApplicationCreate, request: Request
) -> dict[str, Any]:
    user = require_student(request)
    with get_db() as db:
        student = _get_or_create_student(db, user.id)
        app_obj = Application(
            student_id=student.id,
            job_title=req.job_title or req.title,
            company=req.company,
            url=req.url,
            notes=req.notes,
            status=_resolve_application_status(req.status),
            applied_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(app_obj)
        student.last_activity_at = datetime.utcnow()
        db.commit()
        db.refresh(app_obj)
        return _serialize_application(app_obj)


@app.patch("/api/student/applications/{app_id}")
@limiter.limit("30/minute")
def update_application(
    app_id: int, req: ApplicationUpdate, request: Request
) -> dict[str, Any]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            raise HTTPException(404, "Profil etudiant introuvable")
        app_obj = db.query(Application).filter(Application.id == app_id).first()
        if not app_obj:
            raise HTTPException(404, "Candidature introuvable")
        if app_obj.student_id != student.id:
            raise HTTPException(403, "Acces refuse")
        app_obj.status = STATUS_MAP.get(req.status, req.status)
        app_obj.notes = req.notes
        app_obj.updated_at = datetime.utcnow()
        student.last_activity_at = datetime.utcnow()
        db.commit()
        db.refresh(app_obj)
        return _serialize_application(app_obj)


@app.delete("/api/student/applications/{app_id}")
def delete_application(app_id: int, request: Request) -> dict[str, str]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            raise HTTPException(404, "Profil etudiant introuvable")
        app_obj = db.query(Application).filter(Application.id == app_id).first()
        if not app_obj:
            raise HTTPException(404, "Candidature introuvable")
        if app_obj.student_id != student.id:
            raise HTTPException(403, "Acces refuse")
        db.delete(app_obj)
        db.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Student status update
# ---------------------------------------------------------------------------
@app.patch("/api/student/status")
@limiter.limit("10/minute")
def update_student_status(
    req: StatusUpdate, request: Request
) -> dict[str, str]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            raise HTTPException(404, "Profil etudiant introuvable")
        student.stage_status = req.stage_status
        student.stage_company = req.stage_company
        student.last_activity_at = datetime.utcnow()
        db.commit()
    return {
        "status": "ok",
        "stage_status": req.stage_status,
        "stage_company": req.stage_company,
    }


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------
@app.get("/api/admin/students")
def admin_students(request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    school_id_param = request.query_params.get("school_id")
    with get_db() as db:
        query = db.query(Student)
        if school_id_param:
            try:
                query = query.filter(Student.school_id == int(school_id_param))
            except ValueError:
                raise HTTPException(400, "school_id invalide")
        students = query.limit(MAX_ADMIN_STUDENTS).all()

        # Batch-load related data to avoid N+1 queries
        user_ids = [s.user_id for s in students]
        school_ids = [s.school_id for s in students if s.school_id]
        student_ids = [s.id for s in students]

        users_by_id = {
            u.id: u
            for u in db.query(User).filter(User.id.in_(user_ids)).all()
        } if user_ids else {}

        schools_by_id = {
            sc.id: sc
            for sc in db.query(School).filter(School.id.in_(school_ids)).all()
        } if school_ids else {}

        saved_counts: dict[int, int] = {}
        app_counts: dict[int, int] = {}
        if student_ids:
            for sid, cnt in (
                db.query(SavedJob.student_id, func.count(SavedJob.id))
                .filter(SavedJob.student_id.in_(student_ids))
                .group_by(SavedJob.student_id)
                .all()
            ):
                saved_counts[sid] = cnt
            for sid, cnt in (
                db.query(Application.student_id, func.count(Application.id))
                .filter(Application.student_id.in_(student_ids))
                .group_by(Application.student_id)
                .all()
            ):
                app_counts[sid] = cnt

        result = []
        for s in students:
            u = users_by_id.get(s.user_id)
            sc = schools_by_id.get(s.school_id) if s.school_id else None
            result.append({
                "id": s.id,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "email": u.email if u else "",
                "school_id": s.school_id,
                "school_name": sc.name if sc else "",
                "promo": s.promo,
                "target_sector": s.target_sector,
                "stage_status": s.stage_status,
                "stage_company": s.stage_company,
                "domain_found": s.domain_found,
                "cv_filename": s.cv_filename,
                "has_cv": bool(s.cv_filename),
                "saved_jobs_count": saved_counts.get(s.id, 0),
                "applications_count": app_counts.get(s.id, 0),
                "last_activity_at": (
                    s.last_activity_at.isoformat() if s.last_activity_at else None
                ),
                "notes": s.notes or "",
            })
        return result


@app.patch("/api/admin/students/{student_id}")
@limiter.limit("30/minute")
def admin_update_student(
    student_id: int, req: AdminStudentUpdate, request: Request
) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(404, "Etudiant introuvable")
        if req.domain_found is not None:
            student.domain_found = req.domain_found
        if req.stage_status is not None and req.stage_status in VALID_STAGE_STATUSES:
            student.stage_status = req.stage_status
        if req.stage_company is not None:
            student.stage_company = req.stage_company
        if req.notes is not None:
            student.notes = req.notes
        if req.promo is not None:
            student.promo = req.promo
        if req.school_id is not None:
            student.school_id = req.school_id
        if req.target_sector is not None:
            student.target_sector = req.target_sector
        db.commit()
        db.refresh(student)
    return {"status": "ok", "id": student.id}


@app.post("/api/admin/students/{student_id}/reset-password")
@limiter.limit("20/minute")
def admin_reset_student_password(
    student_id: int, request: Request
) -> dict[str, Any]:
    """Reset a student's password to a temporary one."""
    require_admin(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(404, "Etudiant introuvable")
        user = db.query(User).filter(User.id == student.user_id).first()
        if not user:
            raise HTTPException(404, "Utilisateur introuvable")
        temp_password = f"icart{random.randint(1000, 9999)}"
        user.password_hash = bcrypt.hashpw(
            temp_password.encode(), bcrypt.gensalt()
        ).decode()
        db.commit()
    return {
        "status": "ok",
        "temp_password": temp_password,
        "message": f"Mot de passe reinitialise pour {student.first_name} {student.last_name}",
    }


@app.get("/api/admin/students/{student_id}/cv")
def admin_serve_student_cv(student_id: int, request: Request) -> FileResponse:
    require_admin(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.id == student_id).first()
        if not student or not student.cv_filename:
            raise HTTPException(404, "Aucun CV trouve")
        filepath = _safe_filepath(student.cv_filename)
        if not os.path.exists(filepath):
            raise HTTPException(404, "Fichier CV introuvable")
        return FileResponse(
            filepath, media_type="application/pdf", filename=student.cv_filename
        )


@app.get("/api/admin/stats")
def admin_stats(request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    with get_db() as db:
        schools = (
            db.query(School)
            .filter(School.active == True)  # noqa: E712
            .all()
        )
        school_ids = [sc.id for sc in schools]

        # Single query to get counts per school and status
        counts: dict[int, dict[str, int]] = {
            sc_id: {"total": 0, "found": 0, "searching": 0} for sc_id in school_ids
        }
        if school_ids:
            rows = (
                db.query(
                    Student.school_id,
                    Student.stage_status,
                    func.count(Student.id),
                )
                .filter(Student.school_id.in_(school_ids))
                .group_by(Student.school_id, Student.stage_status)
                .all()
            )
            for school_id, status, cnt in rows:
                if school_id in counts:
                    counts[school_id]["total"] += cnt
                    if status == "found":
                        counts[school_id]["found"] += cnt
                    elif status == "searching":
                        counts[school_id]["searching"] += cnt

        return [
            {
                "school_id": sc.id,
                "school_name": sc.name,
                "total": counts[sc.id]["total"],
                "found": counts[sc.id]["found"],
                "searching": counts[sc.id]["searching"],
            }
            for sc in schools
        ]


# ---------------------------------------------------------------------------
# Admin search (scraping for admin)
# ---------------------------------------------------------------------------
@app.post("/api/admin/search")
@limiter.limit("5/minute")
def admin_search(req: ScrapeRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    keywords = req.keywords.strip()
    sector = req.sector.strip()
    location = req.location.strip() or "Paris"

    if not keywords:
        raise HTTPException(400, "Mots-cles requis")

    all_jobs = _run_scrape(keywords, sector, location)

    if not all_jobs:
        return {
            "jobs": [],
            "count": 0,
            "message": "Aucune offre trouvee. Essayez d'autres mots-cles.",
        }

    all_jobs = score_jobs_with_groq(all_jobs, "", sector)
    all_jobs.sort(key=lambda x: x.get("score", 0), reverse=True)

    return {"jobs": _format_jobs(all_jobs), "count": len(all_jobs)}


class RecommendJobRequest(BaseModel):
    student_id: int
    job_title: str = Field(..., max_length=500)
    job_company: str = Field("", max_length=200)
    job_url: str = Field("", max_length=2000)
    job_description: str = Field("", max_length=2000)


@app.post("/api/admin/recommend-job")
@limiter.limit("30/minute")
def admin_recommend_job(
    req: RecommendJobRequest, request: Request
) -> dict[str, Any]:
    user = require_admin(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.id == req.student_id).first()
        if not student:
            raise HTTPException(404, "Etudiant introuvable")

        # Build a message with job details
        parts = [f"Offre recommandee pour vous :"]
        parts.append(f"{req.job_title}")
        if req.job_company:
            parts.append(f"Entreprise : {req.job_company}")
        if req.job_url:
            parts.append(f"Lien : {req.job_url}")
        if req.job_description:
            desc_preview = req.job_description[:300]
            parts.append(f"Description : {desc_preview}")

        content = "\n".join(parts)

        msg = Message(
            from_user_id=user.id,
            to_student_id=req.student_id,
            content=content,
            sent_at=datetime.utcnow(),
            read=0,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return {
            "success": True,
            "message": "Offre envoyee a l'etudiant par message",
        }


# ---------------------------------------------------------------------------
# CSV Import
# ---------------------------------------------------------------------------
@app.post("/api/admin/import-csv")
@limiter.limit("10/minute")
def admin_import_csv(
    request: Request,
    file: UploadFile = File(...),
    school_id: int = 0,
) -> dict[str, Any]:
    """Import students from a CSV file.

    Expected CSV columns (flexible headers, case-insensitive):
    - nom / last_name
    - prenom / first_name
    - email
    - promotion / promo (optional)
    - secteur / sector / target_sector (optional)
    - notes (optional)
    """
    require_admin(request)

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Le fichier doit etre un CSV (.csv)")

    raw = file.file.read(2 * 1024 * 1024)  # max 2MB
    if len(raw) >= 2 * 1024 * 1024:
        raise HTTPException(400, "Le fichier CSV est trop volumineux (max 2 Mo)")

    # Try different encodings
    content = None
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            content = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise HTTPException(400, "Encodage du fichier non reconnu")

    reader = csv.DictReader(io.StringIO(content), delimiter=None)
    # Auto-detect delimiter
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(content[:2048])
        reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    except csv.Error:
        reader = csv.DictReader(io.StringIO(content))

    # Normalize headers
    HEADER_MAP = {
        "nom": "last_name", "last_name": "last_name", "lastname": "last_name",
        "prenom": "first_name", "first_name": "first_name", "firstname": "first_name",
        "email": "email", "mail": "email", "adresse email": "email",
        "promotion": "promo", "promo": "promo", "classe": "promo",
        "secteur": "sector", "sector": "sector", "target_sector": "sector",
        "notes": "notes", "commentaires": "notes", "remarques": "notes",
    }

    created = 0
    skipped = 0
    errors_list = []

    with get_db() as db:
        # Resolve school_id from query param
        target_school_id = school_id if school_id > 0 else None

        for row_num, row in enumerate(reader, start=2):
            # Map headers
            mapped = {}
            for key, val in row.items():
                if key is None:
                    continue
                norm = key.strip().lower().replace("_", " ").replace("-", " ").strip().replace(" ", "_")
                # Try direct mapping
                for alias, field in HEADER_MAP.items():
                    if alias.replace(" ", "_") == norm or alias == norm:
                        mapped[field] = (val or "").strip()
                        break

            first_name = mapped.get("first_name", "")
            last_name = mapped.get("last_name", "")
            email = mapped.get("email", "")

            if not email and not (first_name and last_name):
                skipped += 1
                continue

            # Auto-generate email if missing
            if not email:
                email = f"{first_name.lower()}.{last_name.lower()}@icart.fr"
                email = re.sub(r"[^a-z0-9.@]", "", email)

            # Check duplicate
            existing = db.query(User).filter(User.email == email).first()
            if existing:
                skipped += 1
                continue

            # Create user
            password_hash = hash_password("icart2025")
            user = User(
                email=email,
                password_hash=password_hash,
                role="student",
                first_name=first_name,
                last_name=last_name,
            )
            db.add(user)
            db.flush()

            # Create student profile
            student = Student(
                user_id=user.id,
                first_name=first_name,
                last_name=last_name,
                promo=mapped.get("promo", ""),
                school_id=target_school_id,
                target_sector=mapped.get("sector", ""),
                stage_status="searching",
                notes=mapped.get("notes", ""),
                last_activity_at=datetime.utcnow(),
            )
            db.add(student)
            created += 1

        db.commit()

    return {
        "success": True,
        "created": created,
        "skipped": skipped,
        "message": f"{created} etudiant(s) importe(s), {skipped} ignore(s) (doublons ou lignes vides)",
    }


# ---------------------------------------------------------------------------
# Messaging (admin <-> student)
# ---------------------------------------------------------------------------
@app.post("/api/admin/messages")
@limiter.limit("30/minute")
def admin_send_message(
    req: SendMessageRequest, request: Request
) -> dict[str, Any]:
    user = require_admin(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.id == req.to_student_id).first()
        if not student:
            raise HTTPException(404, "Etudiant introuvable")
        msg = Message(
            from_user_id=user.id,
            to_student_id=req.to_student_id,
            content=req.content,
            sent_at=datetime.utcnow(),
            read=0,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return _serialize_message(msg, viewer_user_id=student.user_id)


@app.get("/api/admin/messages/{student_id}")
def admin_get_messages(
    student_id: int, request: Request
) -> list[dict[str, Any]]:
    require_admin(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.id == student_id).first()
        student_user_id = student.user_id if student else -1
        messages = (
            db.query(Message)
            .filter(Message.to_student_id == student_id)
            .order_by(Message.sent_at.asc())
            .limit(500)
            .all()
        )
        return [_serialize_message(m, student_user_id) for m in messages]


@app.post("/api/admin/seed-fake-data")
@limiter.limit("3/hour")
def admin_seed_fake_data(request: Request) -> dict[str, Any]:
    """Seed database with ~50 fake students per school for demo purposes."""
    require_admin(request)

    _FIRST_NAMES = [
        "Emma", "Louise", "Alice", "Chloe", "Lea", "Manon", "Camille", "Jade",
        "Lina", "Sarah", "Juliette", "Clara", "Margaux", "Ines", "Anna",
        "Marie", "Lucie", "Charlotte", "Zoe", "Eva", "Romane", "Agathe",
        "Mathilde", "Victoria", "Elsa", "Noemie", "Hugo", "Lucas", "Gabriel",
        "Louis", "Raphael", "Arthur", "Jules", "Adam", "Mael", "Leo",
        "Nathan", "Paul", "Tom", "Ethan", "Theo", "Maxime", "Alexandre",
        "Antoine", "Valentin", "Baptiste", "Clement", "Oscar", "Samuel",
        "Axel", "Romain", "Victor", "Simon", "Adrien", "Bastien", "Florian",
        "Dylan", "Tristan", "Nolan", "Quentin", "Damien", "Eliot", "Gabin",
        "Liam", "Mathis", "Robin", "Martin", "Enzo", "Noah", "Eliott",
        "Margot", "Pauline", "Oceane", "Anais", "Apolline", "Capucine",
        "Celeste", "Diane", "Elena", "Faustine", "Gabrielle", "Helene",
        "Iris", "Jeanne", "Lola", "Madeleine", "Nina", "Olivia", "Penelope",
        "Rose", "Sophie", "Victoire", "Yasmine", "Ambre", "Blanche",
        "Constance", "Doriane", "Emilie", "Flora", "Garance",
    ]
    _LAST_NAMES = [
        "Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit",
        "Durand", "Leroy", "Moreau", "Simon", "Laurent", "Lefebvre", "Michel",
        "Garcia", "David", "Bertrand", "Roux", "Vincent", "Fournier",
        "Morel", "Girard", "Andre", "Mercier", "Dupont", "Lambert", "Bonnet",
        "Francois", "Martinez", "Legrand", "Garnier", "Faure", "Rousseau",
        "Blanc", "Guerin", "Muller", "Henry", "Roussel", "Nicolas", "Perrin",
        "Morin", "Mathieu", "Clement", "Gauthier", "Dumont", "Lopez",
        "Fontaine", "Chevalier", "Robin", "Masson", "Sanchez", "Noel",
        "Dufour", "Blanchard", "Brunet", "Giraud", "Riviere", "Arnaud",
        "Collet", "Lemoine", "Marchand", "Picard", "Renard", "Barbier",
    ]
    _SECTORS = [
        "Arts visuels & Mediation", "Musique & Spectacle vivant",
        "Cinema & Audiovisuel", "Mode & Luxe", "Communication & Digital",
        "Marche de l'art", "Patrimoine & Museologie",
    ]
    _PROMOS = ["Bachelor 1", "Bachelor 2", "Bachelor 3", "MBA 1", "MBA 2"]
    _COMPANIES = [
        "Musee du Louvre", "Centre Pompidou", "Palais de Tokyo",
        "Fondation Louis Vuitton", "Musee d'Orsay", "Grand Palais",
        "Galerie Perrotin", "Christie's Paris", "Sotheby's France",
        "Artcurial", "LVMH", "Kering", "Chanel", "Dior Couture",
        "Hermes International", "Balenciaga", "Saint Laurent Paris",
        "Canal+", "France Televisions", "Arte", "Gaumont", "Pathe",
        "Philharmonie de Paris", "Opera de Paris", "Theatre du Chatelet",
        "Publicis", "Havas", "BETC", "Galeries Lafayette", "Le Bon Marche",
    ]
    _JOB_TITLES = [
        "Assistant curateur", "Charge de production culturelle",
        "Assistant communication musee", "Coordinateur evenementiel",
        "Assistant galerie d'art", "Charge de mediation culturelle",
        "Assistant marketing luxe", "Coordinateur artistique",
        "Assistant production audiovisuelle", "Charge de relations presse",
        "Community manager culture", "Assistant commissaire d'exposition",
        "Charge de programmation", "Assistant direction artistique",
        "Coordinateur projets culturels", "Assistant patrimoine",
    ]
    _STATUSES = ["a_envoyer", "envoyee", "relance", "entretien", "refusee", "obtenu"]
    _MSG_ADMIN = [
        "Bonjour, comment avancent vos recherches de stage ?",
        "N'hesitez pas a postuler sur les offres que je vous ai envoyees.",
        "Votre CV est bien recu, je le transmets a mon reseau.",
        "Avez-vous eu des retours suite a vos candidatures ?",
        "Pensez a relancer les entreprises ou vous avez postule.",
        "Bravo pour votre entretien ! Tenez-moi au courant.",
    ]
    _MSG_STUDENT = [
        "Merci pour l'offre, je vais postuler !",
        "J'ai envoye ma candidature ce matin.",
        "J'ai un entretien prevu la semaine prochaine !",
        "Malheureusement je n'ai pas ete retenu(e).",
        "J'ai trouve mon stage, merci pour votre aide !",
        "Je cherche encore, mais j'ai quelques pistes.",
    ]

    random.seed(42)
    with get_db() as db:
        existing = db.query(Student).count()
        if existing > 10:
            return {"status": "skip", "message": f"Deja {existing} etudiants en base."}

        admin = db.query(User).filter(User.role == "admin").first()
        if not admin:
            raise HTTPException(400, "Aucun admin trouve")

        schools = db.query(School).all()
        if not schools:
            raise HTTPException(400, "Aucune ecole trouvee")

        now = datetime.utcnow()
        count = 0

        # Create fake jobs
        job_ids = []
        for i in range(40):
            job = Job(
                title=random.choice(_JOB_TITLES),
                company=random.choice(_COMPANIES),
                location=random.choice(["Paris", "Paris 8e", "Paris 3e", "Bordeaux", "Lyon"]),
                url=f"https://example.com/job/{i+1}",
                platform=random.choice(["LinkedIn", "WTTJ", "France Travail", "Profilculture"]),
                sector=random.choice(_SECTORS),
                description=f"Stage dans le secteur culturel.",
                contract_type=random.choice(["Stage", "Alternance"]),
                score=round(random.uniform(60, 95), 1),
                scraped_at=now - timedelta(days=random.randint(0, 30)),
            )
            db.add(job)
            db.flush()
            job_ids.append(job.id)

        used_emails: set[str] = set()
        used_names: set[tuple[str, str]] = set()

        for school in schools:
            for _ in range(random.randint(45, 55)):
                while True:
                    fn = random.choice(_FIRST_NAMES)
                    ln = random.choice(_LAST_NAMES)
                    if (fn, ln) not in used_names:
                        used_names.add((fn, ln))
                        break

                email = f"{fn.lower()}.{ln.lower()}@icart.fr"
                suffix = 1
                while email in used_emails:
                    email = f"{fn.lower()}.{ln.lower()}{suffix}@icart.fr"
                    suffix += 1
                used_emails.add(email)

                is_found = random.random() < 0.30
                hashed = bcrypt.hashpw("icart2025".encode(), bcrypt.gensalt()).decode()
                user = User(
                    email=email, password_hash=hashed, role="student",
                    first_name=fn, last_name=ln,
                )
                db.add(user)
                db.flush()

                last_activity = now - timedelta(hours=random.randint(0, 72), minutes=random.randint(0, 59))
                student = Student(
                    user_id=user.id, first_name=fn, last_name=ln,
                    promo=random.choice(_PROMOS), school_id=school.id,
                    target_sector=random.choice(_SECTORS),
                    stage_status="found" if is_found else "searching",
                    stage_company=random.choice(_COMPANIES) if is_found else "",
                    domain_found=random.choice(_SECTORS) if is_found else "",
                    last_activity_at=last_activity,
                    notes=random.choice(["", "", "", "Tres motive.", "A besoin d'aide pour son CV.", "Bilingue anglais."]),
                )
                db.add(student)
                db.flush()

                # Applications
                for a in range(random.randint(1, 6)):
                    app_status = "obtenu" if (is_found and a == 0) else random.choice(_STATUSES)
                    applied_date = now - timedelta(days=random.randint(0, 45))
                    db.add(Application(
                        student_id=student.id, job_title=random.choice(_JOB_TITLES),
                        company=random.choice(_COMPANIES),
                        url=f"https://example.com/apply/{random.randint(1000, 9999)}",
                        status=app_status, notes="",
                        applied_at=applied_date,
                        updated_at=applied_date + timedelta(days=random.randint(0, 10)),
                    ))

                # Saved jobs
                for jid in random.sample(job_ids, min(random.randint(0, 5), len(job_ids))):
                    db.add(SavedJob(student_id=student.id, job_id=jid, saved_at=now - timedelta(days=random.randint(0, 20))))

                # Messages
                for _ in range(random.randint(0, 8)):
                    msg_time = now - timedelta(days=random.randint(0, 30), hours=random.randint(0, 23))
                    if random.random() < 0.5:
                        db.add(Message(from_user_id=admin.id, to_student_id=student.id,
                                       content=random.choice(_MSG_ADMIN), sent_at=msg_time, read=1 if random.random() < 0.7 else 0))
                    else:
                        db.add(Message(from_user_id=user.id, to_student_id=student.id,
                                       content=random.choice(_MSG_STUDENT), sent_at=msg_time, read=1 if random.random() < 0.8 else 0))

                count += 1

        db.commit()
        return {"status": "ok", "message": f"{count} etudiants crees dans {len(schools)} ecoles."}


@app.get("/api/student/messages")
def student_get_messages(request: Request) -> list[dict[str, Any]]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            raise HTTPException(404, "Profil etudiant introuvable")
        messages = (
            db.query(Message)
            .filter(Message.to_student_id == student.id)
            .order_by(Message.sent_at.asc())
            .limit(500)
            .all()
        )
        # Mark unread messages from admin as read
        db.query(Message).filter(
            Message.to_student_id == student.id,
            Message.from_user_id != user.id,
            Message.read == 0,
        ).update({"read": 1})
        db.commit()
        return [_serialize_message(m, user.id) for m in messages]


@app.post("/api/student/messages")
@limiter.limit("20/minute")
def student_send_message(
    req: StudentMessageRequest, request: Request
) -> dict[str, Any]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            raise HTTPException(404, "Profil etudiant introuvable")
        msg = Message(
            from_user_id=user.id,
            to_student_id=student.id,
            content=req.content,
            sent_at=datetime.utcnow(),
            read=0,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return _serialize_message(msg, user.id)


@app.get("/api/student/messages/unread-count")
def student_unread_count(request: Request) -> dict[str, int]:
    user = require_student(request)
    with get_db() as db:
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if not student:
            raise HTTPException(404, "Profil etudiant introuvable")
        count = (
            db.query(Message)
            .filter(
                Message.to_student_id == student.id,
                Message.from_user_id != user.id,
                Message.read == 0,
            )
            .count()
        )
    return {"count": count}


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------
@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse("static/index.html")
