import asyncio
import io
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import smtplib
import time
import uuid
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from urllib.parse import quote, unquote, urlparse

import bcrypt

import pdfplumber
import requests
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY not found in .env")


groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Database ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///jobs.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    name = Column(String, default="")
    session_token = Column(String, default="")
    session_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Profile(Base):
    __tablename__ = "profiles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    cv = Column(Text, default="")
    cover_letter = Column(Text, default="")
    goals = Column(Text, default="")
    cvs_json = Column(Text, default="[]")
    cover_letters_json = Column(Text, default="[]")
    language = Column(String, default="fr")
    alert_keywords = Column(Text, default="")
    alert_location = Column(String, default="")
    smtp_email = Column(String, default="")
    smtp_password = Column(String, default="")
    smtp_host = Column(String, default="smtp.gmail.com")
    smtp_port = Column(Integer, default=587)
    share_token = Column(String, nullable=True)
    dark_mode = Column(Boolean, default=False)


class SavedJob(Base):
    __tablename__ = "saved_jobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    company = Column(String, default="")
    location = Column(String, default="")
    url = Column(String, default="")
    platform = Column(String, default="")
    date = Column(String, default="")
    score = Column(Float, default=0)
    explanation = Column(Text, default="")
    saved_at = Column(DateTime, default=datetime.utcnow)


class GeneratedLetter(Base):
    __tablename__ = "generated_letters"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    job_title = Column(String, default="")
    company = Column(String, default="")
    content = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    job_title = Column(String, default="")
    company = Column(String, default="")
    url = Column(String, default="")
    score = Column(Float, default=0)
    explanation = Column(Text, default="")
    seen = Column(Integer, default=0)  # 0=unseen, 1=seen (Integer for SQLite compat)
    created_at = Column(DateTime, default=datetime.utcnow)


class Application(Base):
    __tablename__ = "applications"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    job_title = Column(String, default="")
    company = Column(String, default="")
    url = Column(String, default="")
    status = Column(String, default="sent")  # sent, followed_up, interview, rejected, waiting
    notes = Column(Text, default="")
    applied_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class PeopleSearchHistory(Base):
    __tablename__ = "people_search_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    keywords_json = Column(Text, default="[]")  # JSON list of keywords
    location = Column(String, default="")
    results_json = Column(Text, default="[]")    # JSON list of results
    result_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class EmailHistory(Base):
    __tablename__ = "email_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    to_email = Column(String, nullable=False)
    subject = Column(String, default="")
    body = Column(Text, default="")
    application_id = Column(Integer, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)


class ScheduledFollowup(Base):
    __tablename__ = "scheduled_followups"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    send_at = Column(DateTime, nullable=False)
    subject = Column(String, default="")
    body = Column(Text, default="")
    sent = Column(Boolean, default=False)


Base.metadata.create_all(engine)

# --- Migration: add new columns if missing ---
try:
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    existing_cols = [c["name"] for c in inspector.get_columns("profiles")]
    with engine.connect() as conn:
        if "cvs_json" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN cvs_json TEXT DEFAULT '[]'"))
        if "cover_letters_json" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN cover_letters_json TEXT DEFAULT '[]'"))
        if "language" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN language VARCHAR DEFAULT 'fr'"))
        if "alert_keywords" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN alert_keywords TEXT DEFAULT ''"))
        if "alert_location" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN alert_location VARCHAR DEFAULT ''"))
        if "smtp_email" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN smtp_email VARCHAR DEFAULT ''"))
        if "smtp_password" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN smtp_password VARCHAR DEFAULT ''"))
        if "smtp_host" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN smtp_host VARCHAR DEFAULT 'smtp.gmail.com'"))
        if "smtp_port" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN smtp_port INTEGER DEFAULT 587"))
        if "share_token" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN share_token VARCHAR"))
        if "dark_mode" not in existing_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN dark_mode BOOLEAN DEFAULT 0"))
        conn.commit()
    # Migration for users table
    existing_user_cols = [c["name"] for c in inspector.get_columns("users")]
    with engine.connect() as conn:
        if "session_expires_at" not in existing_user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN session_expires_at DATETIME"))
        conn.commit()
    logger.info("Migration check done")
except Exception as e:
    logger.warning(f"Migration check: {e}")

# --- FastAPI ---
docs_url = "/docs" if os.getenv("DEBUG") else None
app = FastAPI(title="JobHunter AI", docs_url=docs_url, redoc_url=None)

# --- Security Headers Middleware ---
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# --- CORS ---
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# --- Rate Limiting ---
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- SSRF Protection ---
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "[::1]"}

def validate_url(url: str) -> bool:
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

os.makedirs("static", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- APScheduler for alerts ---
from apscheduler.schedulers.background import BackgroundScheduler

_alert_scheduler = BackgroundScheduler()


def check_alerts():
    """Background job: for each user with alert_keywords, scrape and create alerts for high-score jobs."""
    db = SessionLocal()
    try:
        profiles = db.query(Profile).filter(Profile.alert_keywords != "", Profile.alert_keywords.isnot(None)).all()
        for prof in profiles:
            keywords = (prof.alert_keywords or "").strip()
            location = (prof.alert_location or "France").strip()
            if not keywords:
                continue
            profile_dict = {
                "cv": prof.cv or "",
                "cover_letter": prof.cover_letter or "",
                "goals": prof.goals or "",
            }
            all_jobs = []
            try:
                all_jobs.extend(scrape_linkedin(keywords, location))
            except Exception:
                pass
            try:
                all_jobs.extend(scrape_france_travail(keywords, location))
            except Exception:
                pass
            all_jobs = deduplicate(all_jobs)
            if not all_jobs:
                continue
            all_jobs = score_jobs_with_groq(all_jobs, profile_dict)
            # Get existing alert URLs and saved job URLs to avoid duplicates
            existing_alert_urls = {a.url for a in db.query(Alert).filter(Alert.user_id == prof.user_id).all() if a.url}
            existing_saved_urls = {s.url for s in db.query(SavedJob).filter(SavedJob.user_id == prof.user_id).all() if s.url}
            existing_urls = existing_alert_urls | existing_saved_urls
            new_alerts = []
            for j in all_jobs:
                if j.get("score", 0) >= 8:
                    job_url = j.get("url", "")
                    if job_url and job_url != "#" and job_url in existing_urls:
                        continue
                    # Also check by title+company to catch duplicates without URL
                    dup = db.query(Alert).filter(
                        Alert.user_id == prof.user_id,
                        Alert.job_title == j.get("title", ""),
                        Alert.company == j.get("company", ""),
                    ).first()
                    if dup:
                        continue
                    alert = Alert(
                        user_id=prof.user_id,
                        job_title=j.get("title", ""),
                        company=j.get("company", ""),
                        url=job_url,
                        score=j.get("score", 0),
                        explanation=j.get("explanation", ""),
                    )
                    db.add(alert)
                    new_alerts.append(j)
            db.commit()
            # Send email notification if user has SMTP configured and there are new alerts
            if new_alerts and getattr(prof, "smtp_email", "") and getattr(prof, "smtp_password", ""):
                try:
                    alert_lines = [f"- {a.get('title', '')} chez {a.get('company', '')} (score: {a.get('score', 0)})" for a in new_alerts[:10]]
                    email_body = f"Bonjour,\n\nJobHunter AI a trouvé {len(new_alerts)} nouvelle(s) alerte(s) correspondant à vos critères:\n\n" + "\n".join(alert_lines) + "\n\nConnectez-vous pour voir les détails.\n\nJobHunter AI"
                    _send_email(
                        prof.smtp_email, prof.smtp_password,
                        getattr(prof, "smtp_host", "smtp.gmail.com") or "smtp.gmail.com",
                        getattr(prof, "smtp_port", 587) or 587,
                        prof.smtp_email,
                        f"JobHunter AI - {len(new_alerts)} nouvelle(s) alerte(s)",
                        email_body,
                    )
                    logger.info(f"Alert email sent to user {prof.user_id}")
                except Exception as e:
                    logger.warning(f"Failed to send alert email to user {prof.user_id}: {e}")
            logger.info(f"Alert check done for user {prof.user_id}")
    except Exception as e:
        logger.warning(f"Alert scheduler error: {e}")
    finally:
        db.close()


def _send_email(smtp_email: str, smtp_password: str, smtp_host: str, smtp_port: int, to_email: str, subject: str, body: str):
    """Send an email via SMTP. Raises on failure."""
    # Sanitize headers against injection
    subject = subject.replace("\n", "").replace("\r", "")
    to_email = to_email.replace("\n", "").replace("\r", "")
    msg = MIMEMultipart()
    msg["From"] = smtp_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        server.starttls()
        server.login(smtp_email, smtp_password)
        server.send_message(msg)


def check_followups():
    """Background job: send due scheduled followup emails."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        due = db.query(ScheduledFollowup).filter(
            ScheduledFollowup.sent == False,
            ScheduledFollowup.send_at <= now,
        ).all()
        for followup in due:
            try:
                prof = db.query(Profile).filter(Profile.user_id == followup.user_id).first()
                if not prof or not prof.smtp_email or not prof.smtp_password:
                    logger.warning(f"Followup {followup.id}: no SMTP config for user {followup.user_id}")
                    continue
                app_entry = db.query(Application).filter(Application.id == followup.application_id).first()
                to_email = ""
                if app_entry and app_entry.url:
                    to_email = ""  # We need a recipient; use body as full email
                _send_email(
                    prof.smtp_email, prof.smtp_password,
                    prof.smtp_host or "smtp.gmail.com", prof.smtp_port or 587,
                    prof.smtp_email,  # send to self if no recipient known
                    followup.subject, followup.body,
                )
                followup.sent = True
                # Log in email history
                eh = EmailHistory(
                    user_id=followup.user_id, to_email=prof.smtp_email,
                    subject=followup.subject, body=followup.body,
                    application_id=followup.application_id,
                )
                db.add(eh)
                if app_entry:
                    app_entry.status = "followed_up"
                    app_entry.updated_at = now
                db.commit()
                logger.info(f"Sent followup {followup.id} for user {followup.user_id}")
            except Exception as e:
                logger.warning(f"Followup {followup.id} send error: {e}")
    except Exception as e:
        logger.warning(f"Followup scheduler error: {e}")
    finally:
        db.close()


_alert_scheduler.add_job(check_alerts, "interval", hours=24, id="check_alerts", replace_existing=True)
_alert_scheduler.add_job(check_followups, "interval", hours=1, id="check_followups", replace_existing=True)


@app.on_event("startup")
def start_scheduler():
    if not _alert_scheduler.running:
        _alert_scheduler.start()
        logger.info("Alert scheduler started (runs every 24h)")


@app.on_event("shutdown")
def stop_scheduler():
    if _alert_scheduler.running:
        _alert_scheduler.shutdown(wait=False)


def get_current_user(request: Request) -> Optional[User]:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.session_token == token).first()
        if user and user.session_expires_at and user.session_expires_at < datetime.utcnow():
            return None
        return user
    finally:
        db.close()


def require_user(request: Request) -> User:
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Non authentifié")
    return user


@app.get("/")
def serve_index():
    return FileResponse("static/index.html")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    # Support legacy SHA-256 hashes (64 hex chars) for migration
    if len(hashed) == 64 and all(c in '0123456789abcdef' for c in hashed):
        import hashlib
        if hashlib.sha256(password.encode()).hexdigest() == hashed:
            return True
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


# --- Auth ---
class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)
    name: str = Field("", max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)


@app.post("/api/auth/register")
@limiter.limit("3/minute")
def register(req: RegisterRequest, request: Request):
    if not req.email or not req.password:
        raise HTTPException(400, "Email et mot de passe requis")
    if len(req.password) < 8:
        raise HTTPException(400, "Le mot de passe doit contenir au moins 8 caractères")
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == req.email).first()
        if existing:
            raise HTTPException(400, "Cet email est déjà utilisé")
        session_token = secrets.token_urlsafe(32)
        user = User(
            email=req.email,
            password_hash=hash_password(req.password),
            name=req.name or req.email.split("@")[0],
            session_token=session_token,
            session_expires_at=datetime.utcnow() + timedelta(hours=24),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        profile = Profile(user_id=user.id)
        db.add(profile)
        db.commit()
        return {
            "token": session_token,
            "user": {"name": user.name, "email": user.email},
        }
    finally:
        db.close()


@app.post("/api/auth/login")
@limiter.limit("5/minute")
def login(req: LoginRequest, request: Request):
    if not req.email or not req.password:
        raise HTTPException(400, "Email et mot de passe requis")
    dummy_hash = hash_password("dummy_password_for_timing")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == req.email).first()
        if not user:
            verify_password(req.password, dummy_hash)  # constant time
            raise HTTPException(401, "Email ou mot de passe incorrect")
        if not verify_password(req.password, user.password_hash):
            raise HTTPException(401, "Email ou mot de passe incorrect")
        # Auto-migrate legacy SHA-256 hash to bcrypt
        if not user.password_hash.startswith("$2"):
            user.password_hash = hash_password(req.password)
        session_token = secrets.token_urlsafe(32)
        user.session_token = session_token
        user.session_expires_at = datetime.utcnow() + timedelta(hours=24)
        db.commit()
        return {
            "token": session_token,
            "user": {"name": user.name, "email": user.email},
        }
    finally:
        db.close()


@app.get("/api/auth/me")
def get_me(request: Request):
    user = require_user(request)
    return {"name": user.name, "email": user.email}


@app.post("/api/auth/logout")
def logout(request: Request):
    user = get_current_user(request)
    if not user:
        return {"status": "ok"}
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user.id).first()
        if u:
            u.session_token = ""
            u.session_expires_at = None
            db.commit()
        return {"status": "ok"}
    finally:
        db.close()


# --- Profile ---
class ProfileData(BaseModel):
    cv: str = Field("", max_length=50000)
    cover_letter: str = Field("", max_length=50000)
    goals: str = Field("", max_length=10000)
    cvs: list = []
    cover_letters: list = []
    language: str = Field("fr", max_length=10)
    smtp_email: str = Field("", max_length=200)
    smtp_password: str = Field("", max_length=200)
    smtp_host: str = Field("smtp.gmail.com", max_length=200)
    smtp_port: int = 587
    dark_mode: bool = False


@app.get("/api/profile")
def get_profile(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        p = db.query(Profile).filter(Profile.user_id == user.id).first()
        if not p:
            return {
                "cv": "", "cover_letter": "", "goals": "", "cvs": [], "cover_letters": [],
                "language": "fr", "alert_keywords": "", "alert_location": "",
                "completion_score": 0, "completion_message": "Commencez par ajouter votre CV pour personnaliser votre experience.",
            }
        cvs = []
        cover_letters = []
        try:
            cvs = json.loads(p.cvs_json or "[]")
        except Exception:
            pass
        try:
            cover_letters = json.loads(p.cover_letters_json or "[]")
        except Exception:
            pass
        # Migrate old single cv/cover_letter into arrays if arrays are empty
        if not cvs and p.cv:
            cvs = [{"name": "Mon CV", "content": p.cv}]
        if not cover_letters and p.cover_letter:
            cover_letters = [{"name": "Ma lettre", "content": p.cover_letter}]

        # Calculate completion score
        score = 0
        has_cv = bool(p.cv and p.cv.strip())
        has_cl = bool(p.cover_letter and p.cover_letter.strip())
        has_goals = bool(p.goals and p.goals.strip())
        has_application = db.query(Application).filter(Application.user_id == user.id).first() is not None
        has_saved = db.query(SavedJob).filter(SavedJob.user_id == user.id).first() is not None

        if has_cv:
            score += 30
        if has_cl:
            score += 30
        if has_goals:
            score += 20
        if has_application:
            score += 10
        if has_saved:
            score += 10

        if score >= 90:
            completion_msg = "Excellent ! Votre profil est complet. Vous etes pret pour decrocher le poste ideal !"
        elif score >= 60:
            completion_msg = "Bon progres ! Ajoutez encore quelques elements pour optimiser vos chances."
        elif score >= 30:
            completion_msg = "Bon debut ! Completez votre profil pour obtenir de meilleurs resultats."
        else:
            completion_msg = "Commencez par ajouter votre CV pour personnaliser votre experience."

        return {
            "cv": p.cv or "",
            "cover_letter": p.cover_letter or "",
            "goals": p.goals or "",
            "cvs": cvs,
            "cover_letters": cover_letters,
            "language": p.language or "fr",
            "alert_keywords": getattr(p, "alert_keywords", "") or "",
            "alert_location": getattr(p, "alert_location", "") or "",
            "completion_score": score,
            "completion_message": completion_msg,
            "smtp_email": getattr(p, "smtp_email", "") or "",
            "smtp_password": "********" if (getattr(p, "smtp_password", "") or "") else "",
            "smtp_host": getattr(p, "smtp_host", "smtp.gmail.com") or "smtp.gmail.com",
            "smtp_port": getattr(p, "smtp_port", 587) or 587,
            "dark_mode": bool(getattr(p, "dark_mode", False)),
        }
    finally:
        db.close()


@app.post("/api/profile/save")
def save_profile(data: ProfileData, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        p = db.query(Profile).filter(Profile.user_id == user.id).first()
        # Use first CV/cover_letter for backward compat
        main_cv = data.cvs[0]["content"] if data.cvs else data.cv
        main_cl = data.cover_letters[0]["content"] if data.cover_letters else data.cover_letter
        if not p:
            p = Profile(
                user_id=user.id, cv=main_cv, cover_letter=main_cl, goals=data.goals,
                cvs_json=json.dumps(data.cvs), cover_letters_json=json.dumps(data.cover_letters),
                language=data.language,
                smtp_email=data.smtp_email, smtp_password=data.smtp_password,
                smtp_host=data.smtp_host, smtp_port=data.smtp_port,
                dark_mode=data.dark_mode,
            )
            db.add(p)
        else:
            p.cv = main_cv
            p.cover_letter = main_cl
            p.goals = data.goals
            p.cvs_json = json.dumps(data.cvs)
            p.cover_letters_json = json.dumps(data.cover_letters)
            p.language = data.language
            if data.smtp_email:
                p.smtp_email = data.smtp_email
            if data.smtp_password:
                p.smtp_password = data.smtp_password
            if data.smtp_host:
                p.smtp_host = data.smtp_host
            if data.smtp_port:
                p.smtp_port = data.smtp_port
            p.dark_mode = data.dark_mode
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


@app.post("/api/profile/upload-pdf")
async def upload_pdf(request: Request, file: UploadFile = File(...)):
    """Extract text from a PDF file (CV or cover letter). Returns extracted text without saving."""
    user = require_user(request)

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Seuls les fichiers PDF sont acceptés.")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "Fichier trop volumineux (max 10 Mo).")

    if not content.startswith(b'%PDF'):
        raise HTTPException(400, "Le fichier n'est pas un PDF valide.")

    try:
        pdf_text = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pdf_text += text + "\n"
        if not pdf_text.strip():
            raise HTTPException(400, "Impossible d'extraire le texte du PDF.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        raise HTTPException(400, "Erreur lors de la lecture du PDF.")

    # AI analysis
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "user",
                    "content": f"""Voici le texte extrait d'un document PDF.
Reformate-le proprement en texte structuré et lisible, en gardant toutes les informations importantes.

Texte brut:
{pdf_text[:4000]}

Réponds UNIQUEMENT avec le texte reformaté, sans commentaire ni introduction.""",
                }
            ],
            temperature=0.2,
            max_tokens=2000,
        )
        formatted = resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Groq PDF analysis error: {e}")
        formatted = pdf_text.strip()

    return {"text": formatted, "filename": file.filename, "status": "ok"}


# Keep old endpoint for backward compat
@app.post("/api/profile/upload-cv")
async def upload_cv(request: Request, file: UploadFile = File(...)):
    result = await upload_pdf(request, file)
    return {"cv": result["text"], "status": "ok"}


# --- Scraping ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]


def get_random_headers() -> dict:
    """Return headers with a random User-Agent and realistic browser headers."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": random.choice([
            "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "fr-FR,fr;q=0.9,en;q=0.8",
            "en-US,en;q=0.9,fr-FR;q=0.8,fr;q=0.7",
        ]),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": random.choice([
            "https://www.google.com/",
            "https://www.google.fr/",
            "https://www.bing.com/",
        ]),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


def safe_request(url: str, method: str = "get", **kwargs) -> Optional[requests.Response]:
    """Make an HTTP request with random headers, retry once on 429/403."""
    kwargs.setdefault("headers", get_random_headers())
    kwargs.setdefault("timeout", 12)
    try:
        resp = getattr(requests, method)(url, **kwargs)
        if resp.status_code in (429, 403):
            logger.info(f"Got {resp.status_code} for {url}, waiting 30s and retrying...")
            time.sleep(30)
            kwargs["headers"] = get_random_headers()
            resp = getattr(requests, method)(url, **kwargs)
        return resp
    except Exception as e:
        logger.warning(f"safe_request error for {url}: {e}")
        return None


def detect_contract_type(text: str) -> str:
    """Detect contract type from job title or description text."""
    t = text.upper()
    if "ALTERNANCE" in t or "APPRENTISSAGE" in t:
        return "Alternance"
    if "STAGE" in t or "STAGIAIRE" in t or "INTERN" in t:
        return "Stage"
    if "FREELANCE" in t or "INDÉPENDANT" in t or "INDEPENDANT" in t:
        return "Freelance"
    if "CDD" in t or "CONTRAT À DURÉE DÉTERMINÉE" in t:
        return "CDD"
    if "CDI" in t or "CONTRAT À DURÉE INDÉTERMINÉE" in t:
        return "CDI"
    return ""


def scrape_linkedin(keywords: str, location: str) -> list[dict]:
    jobs = []
    try:
        url = f"https://www.linkedin.com/jobs/search?keywords={quote(keywords)}&location={quote(location)}&f_TPR=r604800"
        resp = safe_request(url)
        if resp is None or resp.status_code != 200:
            return jobs
        soup = BeautifulSoup(resp.content, "lxml")
        cards = soup.find_all("div", class_="base-card")
        for card in cards[:15]:
            title_el = card.find("h3", class_="base-search-card__title")
            company_el = card.find("h4", class_="base-search-card__subtitle")
            loc_el = card.find("span", class_="job-search-card__location")
            link_el = card.find("a", class_="base-card__full-link")
            date_el = card.find("time")
            title = title_el.get_text().strip() if title_el else None
            company = company_el.get_text().strip() if company_el else None
            if not title or not company:
                continue
            if title.count("*") / max(len(title), 1) > 0.3:
                continue
            loc = loc_el.get_text().strip() if loc_el else location
            link = link_el["href"] if link_el else ""
            date_text = ""
            if date_el:
                try:
                    dt = datetime.fromisoformat(date_el["datetime"].replace("Z", "+00:00"))
                    date_text = dt.strftime("%d/%m/%Y")
                except Exception:
                    date_text = date_el.get_text().strip()
            contract = detect_contract_type(title + " " + card.get_text())
            jobs.append({
                "title": title, "company": company, "location": loc,
                "url": link, "platform": "LinkedIn", "date": date_text,
                "contract_type": contract,
            })
        time.sleep(random.uniform(1, 3))
    except Exception as e:
        logger.warning(f"LinkedIn scraping error: {e}")
    return jobs


async def scrape_wttj_async(keywords: str, location: str) -> list[dict]:
    """Scrape Welcome to the Jungle with Playwright (headless browser)."""
    jobs = []
    if not HAS_PLAYWRIGHT:
        logger.warning("Playwright not available, skipping WTTJ")
        return jobs
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(
                f"https://www.welcometothejungle.com/fr/jobs?query={quote(keywords)}",
                timeout=15000,
            )
            await page.wait_for_timeout(4000)

            cards = await page.query_selector_all('li[data-testid="search-results-list-item-wrapper"]')
            for card in cards[:20]:
                try:
                    text = await card.inner_text()
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    # Structure: [optional "Recrute activement !", title, company, description, contract, location]
                    title = ""
                    company = ""
                    loc = location
                    for i, line in enumerate(lines):
                        if line == "Recrute activement !":
                            continue
                        if not title:
                            title = line
                        elif not company:
                            company = line
                            break

                    # Find location from end of lines (usually last or before-last)
                    for line in reversed(lines):
                        if line in ("CDI", "CDD", "Stage", "Alternance", "Freelance", "Recrute activement !"):
                            continue
                        if len(line) < 50:
                            loc = line
                            break

                    link_el = await card.query_selector('a[href*="/jobs/"]')
                    href = ""
                    if link_el:
                        href = await link_el.get_attribute("href") or ""
                        if href.startswith("/"):
                            href = f"https://www.welcometothejungle.com{href}"

                    # Detect contract type from card text
                    full_text = " ".join(lines)
                    contract = detect_contract_type(full_text)

                    if title and title != "Recrute activement !":
                        jobs.append({
                            "title": title, "company": company, "location": loc,
                            "url": href, "platform": "WTTJ", "date": "",
                            "contract_type": contract,
                        })
                except Exception:
                    continue

            await browser.close()
    except Exception as e:
        logger.warning(f"WTTJ Playwright error: {e}")
    return jobs


def scrape_wttj(keywords: str, location: str) -> list[dict]:
    """Sync wrapper for async WTTJ scraper."""
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(scrape_wttj_async(keywords, location))
        loop.close()
        return result
    except Exception as e:
        logger.warning(f"WTTJ scraper error: {e}")
        return []


def scrape_france_travail(keywords: str, location: str) -> list[dict]:
    """Scrape France Travail (ex-Pole Emploi) --- fiable, 20+ resultats."""
    jobs = []
    try:
        url = f"https://candidat.francetravail.fr/offres/recherche?motsCles={quote(keywords)}&offresPartenaires=true"
        resp = safe_request(url)
        if resp is None or resp.status_code != 200:
            return jobs
        soup = BeautifulSoup(resp.content, "lxml")
        for li in soup.select("li.result")[:20]:
            title_el = li.find("h2")
            title = ""
            if title_el:
                span = title_el.find("span", class_="media-heading-title")
                title = span.get_text().strip() if span else title_el.get_text().strip()
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
                link = f"https://candidat.francetravail.fr{href}" if href.startswith("/") else href
            date_el = li.find("p", class_="date")
            date_text = date_el.get_text().strip() if date_el else ""
            full_text = li.get_text()
            contract = detect_contract_type(full_text)
            jobs.append({
                "title": title, "company": company, "location": loc,
                "url": link, "platform": "France Travail", "date": date_text,
                "contract_type": contract,
            })
        time.sleep(random.uniform(0.5, 1))
    except Exception as e:
        logger.warning(f"France Travail error: {e}")
    return jobs


def generate_mock_jobs(keywords: str, location: str) -> list[dict]:
    mock_titles = [
        f"Chargé(e) de {keywords}", f"Responsable {keywords}",
        f"Consultant(e) {keywords}", f"Chef de projet {keywords}",
        f"Coordinateur(trice) {keywords}",
    ]
    mock_companies = ["Accenture", "Capgemini", "BNP Paribas", "Orange", "Société Générale"]
    return [
        {"title": mock_titles[i], "company": mock_companies[i], "location": location,
         "url": "#", "platform": "Démo", "date": datetime.now().strftime("%d/%m/%Y"),
         "contract_type": "CDI"}
        for i in range(5)
    ]


def deduplicate(jobs: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for j in jobs:
        key = (j["title"].lower().strip(), j["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(j)
    return unique


def score_jobs_with_groq(jobs: list[dict], profile: dict) -> list[dict]:
    if not jobs:
        return jobs
    cv = profile.get("cv", "")
    goals = profile.get("goals", "")
    if not cv and not goals:
        for j in jobs:
            j["score"] = round(random.uniform(4, 8), 1)
            j["explanation"] = "Complétez votre profil pour un scoring personnalisé."
        return jobs

    batches = [jobs[i:i + 8] for i in range(0, len(jobs), 8)]
    scored = []
    for batch in batches:
        # Try to fetch full descriptions for jobs with valid URLs
        job_descriptions = {}
        for idx, j in enumerate(batch):
            job_url = j.get("url", "")
            if job_url and job_url != "#":
                try:
                    desc = fetch_job_description(job_url)
                    if desc:
                        job_descriptions[idx] = desc
                except Exception:
                    pass

        job_lines = []
        for idx, j in enumerate(batch):
            line = f"- {j['title']} chez {j['company']} à {j['location']}"
            if idx in job_descriptions:
                line += f"\n  Description: {job_descriptions[idx][:600]}"
            job_lines.append(line)
        job_list_text = "\n".join(job_lines)
        prompt = f"""Tu es un conseiller en emploi. Voici le profil du candidat:
CV: {cv[:1500]}
Objectifs: {goals[:500]}

Voici des offres d'emploi:
{job_list_text}

Pour chaque offre, donne un score de 1 à 10 (10 = correspond parfaitement) et UNE phrase d'explication en français.
Réponds UNIQUEMENT en JSON valide, un tableau d'objets avec "score" (number) et "explanation" (string).
Exemple: [{{"score": 7, "explanation": "Bon match pour vos compétences en gestion de projet."}}]"""

        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=1024,
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
        except Exception as e:
            logger.warning(f"Groq scoring error: {e}")
            for j in batch:
                j["score"] = round(random.uniform(4, 8), 1)
                j["explanation"] = "Scoring IA temporairement indisponible."
        scored.extend(batch)
    return scored


# --- Search ---
class SearchRequest(BaseModel):
    keywords: str = Field(..., max_length=500)
    location: str = Field("France", max_length=200)
    platforms: list[str] = ["linkedin", "wttj", "francetravail"]


@app.post("/api/search")
@limiter.limit("10/minute")
def search_jobs(req: SearchRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
        profile_dict = {
            "cv": profile.cv if profile else "",
            "cover_letter": profile.cover_letter if profile else "",
            "goals": profile.goals if profile else "",
        }
        # Save search keywords for alert system
        if profile and req.keywords:
            profile.alert_keywords = req.keywords
            profile.alert_location = req.location or ""
            db.commit()
    finally:
        db.close()

    all_jobs = []
    is_demo = False

    if "linkedin" in req.platforms:
        all_jobs.extend(scrape_linkedin(req.keywords, req.location))
    if "wttj" in req.platforms:
        all_jobs.extend(scrape_wttj(req.keywords, req.location))
    if "francetravail" in req.platforms:
        all_jobs.extend(scrape_france_travail(req.keywords, req.location))

    all_jobs = deduplicate(all_jobs)

    if not all_jobs:
        all_jobs = generate_mock_jobs(req.keywords, req.location)
        is_demo = True

    all_jobs = score_jobs_with_groq(all_jobs, profile_dict)
    all_jobs.sort(key=lambda x: x.get("score", 0), reverse=True)

    return {"jobs": all_jobs, "is_demo": is_demo, "count": len(all_jobs)}


# --- Saved jobs ---
class SaveJobRequest(BaseModel):
    title: str = Field(..., max_length=500)
    company: str = Field("", max_length=200)
    location: str = Field("", max_length=200)
    url: str = Field("", max_length=2000)
    platform: str = Field("", max_length=200)
    date: str = Field("", max_length=200)
    score: float = 0
    explanation: str = Field("", max_length=10000)


@app.get("/api/jobs/saved")
def get_saved_jobs(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        jobs = db.query(SavedJob).filter(SavedJob.user_id == user.id).order_by(SavedJob.score.desc()).limit(100).all()
        return [
            {"id": j.id, "title": j.title, "company": j.company, "location": j.location,
             "url": j.url, "platform": j.platform, "date": j.date, "score": j.score, "explanation": j.explanation}
            for j in jobs
        ]
    finally:
        db.close()


@app.post("/api/jobs/save")
def toggle_save_job(req: SaveJobRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        existing = db.query(SavedJob).filter(
            SavedJob.user_id == user.id, SavedJob.title == req.title, SavedJob.company == req.company
        ).first()
        if existing:
            db.delete(existing)
            db.commit()
            return {"status": "removed"}
        else:
            job = SavedJob(
                user_id=user.id, title=req.title, company=req.company, location=req.location,
                url=req.url, platform=req.platform, date=req.date, score=req.score, explanation=req.explanation,
            )
            db.add(job)
            db.commit()
            return {"status": "saved"}
    finally:
        db.close()


# --- Letters ---
class LetterRequest(BaseModel):
    job_title: str = Field("", max_length=500)
    company: str = Field("", max_length=200)
    job_description: str = Field("", max_length=10000)
    job_url: str = Field("", max_length=2000)
    job_location: str = Field("", max_length=200)
    job_explanation: str = Field("", max_length=10000)
    instruction: str = Field("", max_length=500)
    letter_language: str = Field("fr", max_length=10)


def fetch_job_description(url: str) -> str:
    """Try to fetch the actual job description from the offer URL."""
    if not url or url == "#":
        return ""
    if not validate_url(url):
        return ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.content, "lxml")
        # Remove scripts and styles
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        # Try common job description selectors
        desc = ""
        for selector in [
            "div.description", "div.job-description", "section.description",
            "div[class*='description']", "div[class*='Description']",
            "article", "div.content", "main",
        ]:
            el = soup.select_one(selector)
            if el and len(el.get_text(strip=True)) > 100:
                desc = el.get_text(separator="\n", strip=True)
                break
        if not desc:
            # Fallback: get the main text content
            body = soup.find("body")
            if body:
                desc = body.get_text(separator="\n", strip=True)
        return desc[:3000]
    except Exception as e:
        logger.warning(f"Failed to fetch job description from {url}: {e}")
        return ""


@app.post("/api/letter")
@limiter.limit("5/minute")
def generate_letter(req: LetterRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    finally:
        db.close()

    if not profile or not profile.cv:
        raise HTTPException(400, "Veuillez d'abord compléter votre profil avec votre CV.")

    # Build full CV text from all CVs
    all_cvs_text = profile.cv or ""
    try:
        cvs_list = json.loads(profile.cvs_json or "[]")
        if cvs_list:
            all_cvs_text = "\n\n---\n\n".join(
                f"[{cv.get('name', 'CV')}]\n{cv.get('content', '')}" for cv in cvs_list if cv.get("content")
            )
    except Exception:
        pass

    # Build cover letter examples from all cover letters
    all_cls_text = profile.cover_letter or ""
    try:
        cls_list = json.loads(profile.cover_letters_json or "[]")
        if cls_list:
            all_cls_text = "\n\n---\n\n".join(
                f"[{cl.get('name', 'Lettre')}]\n{cl.get('content', '')}" for cl in cls_list if cl.get("content")
            )
    except Exception:
        pass

    # Try to fetch the actual job description from URL if not provided
    job_desc = req.job_description
    if not job_desc and req.job_url:
        if not validate_url(req.job_url):
            raise HTTPException(400, "URL non autorisée")
        logger.info(f"Fetching job description from URL: {req.job_url}")
        job_desc = fetch_job_description(req.job_url)

    # Build job context
    job_context = f"Offre d'emploi: {req.job_title} chez {req.company}"
    if req.job_location:
        job_context += f" à {req.job_location}"
    if job_desc:
        job_context += f"\n\nDescription complète du poste:\n{job_desc[:2500]}"
    elif req.job_explanation:
        job_context += f"\n\nRésumé de l'offre: {req.job_explanation}"

    lang = req.letter_language or "fr"
    if lang == "en":
        lang_instruction = "Write the cover letter entirely in ENGLISH. Do NOT write in French."
        lang_label = "in English"
        opening_ban = 'Never start with "I am writing to express my interest".'
    else:
        lang_instruction = "Rédige la lettre entièrement en FRANÇAIS."
        lang_label = "en français"
        opening_ban = 'Ne commence JAMAIS par "Je me permets de vous contacter".'

    prompt = f"""Tu es un expert en rédaction de lettres de motivation.

{lang_instruction}

IMPORTANT: Tu dois impérativement utiliser les informations du CV ci-dessous pour personnaliser la lettre.
Mentionne des expériences, compétences et formations spécifiques du candidat qui correspondent à l'offre.

CV du candidat:
{all_cvs_text[:3000]}

Objectifs du candidat:
{profile.goals[:500] if profile.goals else "Non précisé"}

{"IMPORTANT: Voici des exemples de lettres du candidat. Tu DOIS t'inspirer de leur style, ton et structure:" if all_cls_text else ""}
{all_cls_text[:2000] if all_cls_text else ""}

{job_context}
{f"Instruction spécifique: {req.instruction}" if req.instruction else ""}

Rédige une lettre de motivation de ~280 mots, {lang_label}, en 3 paragraphes.
Ton professionnel mais humain, direct et confiant.
{opening_ban}
Fais des liens CONCRETS entre les expériences du CV et les exigences du poste.
{"Inspire-toi FORTEMENT du style et du ton des lettres exemples fournies." if all_cls_text else ""}"""

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7, max_tokens=1500,
        )
        letter_content = resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Groq letter error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")

    db = SessionLocal()
    try:
        gl = GeneratedLetter(user_id=user.id, job_title=req.job_title, company=req.company, content=letter_content)
        db.add(gl)
        db.commit()
    finally:
        db.close()

    return {"letter": letter_content, "job_title": req.job_title, "company": req.company}


@app.get("/api/letters")
def get_letters(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        letters = db.query(GeneratedLetter).filter(
            GeneratedLetter.user_id == user.id
        ).order_by(GeneratedLetter.created_at.desc()).limit(20).all()
        return [
            {"id": l.id, "job_title": l.job_title, "company": l.company,
             "content": l.content, "created_at": l.created_at.isoformat() if l.created_at else ""}
            for l in letters
        ]
    finally:
        db.close()


# --- Stats ---
@app.get("/api/stats")
def get_stats(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        saved_count = db.query(SavedJob).filter(SavedJob.user_id == user.id).count()
        letters_count = db.query(GeneratedLetter).filter(GeneratedLetter.user_id == user.id).count()

        # Applications by status
        applications = db.query(Application).filter(Application.user_id == user.id).all()
        status_counts = {}
        for a in applications:
            status_counts[a.status] = status_counts.get(a.status, 0) + 1

        # Unseen alerts
        unseen_count = db.query(Alert).filter(Alert.user_id == user.id, Alert.seen == 0).count()
        recent_alerts = db.query(Alert).filter(
            Alert.user_id == user.id, Alert.seen == 0
        ).order_by(Alert.created_at.desc()).limit(3).all()
        recent_alerts_data = [
            {"id": a.id, "job_title": a.job_title, "company": a.company, "score": a.score}
            for a in recent_alerts
        ]

        return {
            "total_found": 0,
            "saved": saved_count,
            "letters": letters_count,
            "applications_by_status": status_counts,
            "unseen_alerts": unseen_count,
            "recent_alerts": recent_alerts_data,
        }
    finally:
        db.close()


# --- Chat ---
class ChatRequest(BaseModel):
    message: str = Field(..., max_length=10000)


@app.post("/api/chat")
@limiter.limit("20/minute")
def chat(req: ChatRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
        saved_jobs = db.query(SavedJob).filter(SavedJob.user_id == user.id).all()
    finally:
        db.close()

    profile_ctx = ""
    if profile and profile.cv:
        profile_ctx = f"CV du candidat: {profile.cv[:800]}\nObjectifs: {profile.goals[:300] if profile.goals else 'Non précisé'}\n"

    saved_ctx = ""
    if saved_jobs:
        titles = [f"- {j.title} chez {j.company}" for j in saved_jobs[:10]]
        saved_ctx = f"Offres sauvegardées:\n" + "\n".join(titles) + "\n"

    system_msg = f"""Tu es l'assistant JobHunter AI, un coach carrière amical et professionnel.
Tu réponds toujours en français, de manière concise et encourageante.
Tu aides le candidat dans sa recherche d'emploi.

{profile_ctx}
{saved_ctx}"""

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": req.message},
            ],
            temperature=0.7, max_tokens=600,
        )
        return {"response": resp.choices[0].message.content.strip()}
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")


# --- People Search ---
class PeopleSearchRequest(BaseModel):
    keywords: list = []       # list of up to 3 keyword strings
    keyword: str = Field("", max_length=200)
    location: str = Field("", max_length=200)


def _parse_linkedin_title(title_text: str) -> dict:
    """Parse a LinkedIn search result title into name/title/company."""
    title_clean = title_text.replace(" | LinkedIn", "").replace(" - LinkedIn", "").replace(" – LinkedIn", "")
    parts = [p.strip() for p in title_clean.split(" - ")]
    name = parts[0] if parts else title_text
    title_role = parts[1] if len(parts) > 1 else ""
    company = parts[2] if len(parts) > 2 else ""
    if not company and " chez " in title_role:
        role_parts = title_role.split(" chez ")
        title_role = role_parts[0]
        company = role_parts[1] if len(role_parts) > 1 else ""
    if not company and " at " in title_role:
        role_parts = title_role.split(" at ")
        title_role = role_parts[0]
        company = role_parts[1] if len(role_parts) > 1 else ""
    return {"name": name, "title": title_role, "company": company}


def _extract_location(snippet: str, fallback: str) -> str:
    """Try to extract location from snippet text."""
    loc_patterns = [
        r"Location:\s*([\w\s,.-]+?)(?:\s*·|\s*$)",
        r"([\w\s-]+,\s*[\w\s-]+(?:,\s*[\w\s-]+)?)\s*[·\-]",
        r"Région de ([\w\s-]+)",
        r"(Paris|Lyon|Marseille|Toulouse|Bordeaux|Lille|Nantes|Strasbourg|Nice|Montpellier|[\w\s]+, France)",
    ]
    for pattern in loc_patterns:
        m = re.search(pattern, snippet)
        if m:
            return m.group(1).strip()
    return fallback


def _search_ddgs(query: str, max_results: int = 80) -> list[dict]:
    """Search using ddgs library, return raw results. Splits concatenated titles."""
    try:
        from ddgs import DDGS
        raw_results = DDGS().text(query, max_results=max_results)
        logger.info(f"DDGS: '{query}' -> {len(raw_results)} raw results")
        # ddgs sometimes concatenates multiple LinkedIn results into one title
        # e.g. "Name1 - Title1 | LinkedIn Name2 - Title2 | LinkedIn Name3 ..."
        # Also: "Name1 - Title1 ... Name2 - Title2 ..."  (with ... as separator)
        expanded = []
        for r in raw_results:
            title = r.get("title", "")
            href = r.get("href", "")
            body = r.get("body", "")
            # Split on "| LinkedIn" or "- LinkedIn" boundaries
            if "LinkedIn" in title and (title.count("LinkedIn") > 1 or
                    re.search(r'LinkedIn\s+[A-ZÀ-Ÿ]', title)):
                parts = re.split(r'\s*(?:\||-|–)\s*LinkedIn\s+', title)
                # Last part might end with "| LinkedIn"
                for i, part in enumerate(parts):
                    part = re.sub(r'\s*(?:\||-|–)\s*LinkedIn\s*$', '', part).strip()
                    if not part or len(part) < 3:
                        continue
                    expanded.append({"title": part, "href": href, "body": body})
            # Also split on "... Name - Title ..." patterns (ellipsis concatenation)
            elif title.count("...") >= 2:
                segments = re.split(r'\.\.\.\s+', title)
                for seg in segments:
                    seg = seg.strip().rstrip(".")
                    if not seg or len(seg) < 5 or " - " not in seg:
                        continue
                    expanded.append({"title": seg, "href": href, "body": body})
            else:
                expanded.append(r)
        logger.info(f"DDGS: expanded to {len(expanded)} results")
        return expanded
    except Exception as e:
        logger.warning(f"DDGS error for '{query}': {e}")
        return []


def _search_bing(query: str) -> list[dict]:
    """Fallback Bing scraping, return list of {title, href, snippet}."""
    results = []
    try:
        for start in [0, 50]:
            bing_url = f"https://www.bing.com/search?q={quote(query)}&count=50&first={start}"
            resp = requests.get(bing_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }, timeout=15)
            if resp.status_code != 200:
                break
            soup = BeautifulSoup(resp.content, "lxml")
            for li in soup.select("li.b_algo"):
                link_el = li.find("a", href=True)
                if not link_el:
                    continue
                snippet_el = li.find("p") or li.find("div", class_="b_caption")
                results.append({
                    "title": link_el.get_text(strip=True),
                    "href": link_el["href"],
                    "body": snippet_el.get_text(strip=True) if snippet_el else "",
                })
            time.sleep(random.uniform(0.3, 0.8))
        logger.info(f"Bing: '{query}' -> {len(results)} results")
    except Exception as e:
        logger.warning(f"Bing error for '{query}': {e}")
    return results


def scrape_linkedin_people(keywords_list: list[str], location: str) -> list[dict]:
    """Search for people on LinkedIn using multiple keywords."""
    people = []
    seen_urls = set()

    def add_person(name, title_role, company, loc, href, snippet=""):
        if href in seen_urls:
            return
        if "?" in href:
            href = href.split("?")[0]
        if "/in/" not in href:
            return
        seen_urls.add(href)
        people.append({
            "name": name, "title": title_role, "company": company,
            "location": loc, "linkedin_url": href, "snippet": snippet[:200],
        })

    def process_results(raw_results, loc_fallback):
        for r in raw_results:
            href = r.get("href", "")
            title_text = r.get("title", "")
            snippet = r.get("body", "")
            # Try to find linkedin URL in href or body
            if "linkedin.com/in/" not in href:
                # Try to extract from body/snippet
                li_match = re.search(r'https?://[a-z]*\.?linkedin\.com/in/[\w-]+', snippet + " " + href)
                if li_match:
                    href = li_match.group(0)
                else:
                    continue
            parsed = _parse_linkedin_title(title_text)
            # Skip if name looks like an ad or not a person
            if not parsed["name"] or len(parsed["name"]) < 2:
                continue
            if any(skip in parsed["name"].lower() for skip in ["recrutement", "formez", "formation", "ecole", "école"]):
                continue
            loc = _extract_location(snippet, loc_fallback)
            add_person(parsed["name"], parsed["title"], parsed["company"], loc, href, snippet)

    # Build queries — multiple variations for more results
    queries = []

    # Combined query with all keywords (most precise)
    all_kw = [k.strip() for k in keywords_list[:3] if k.strip()]
    if all_kw:
        combined_exact = " ".join(f'"{k}"' for k in all_kw)
        q = f'site:linkedin.com/in/ {combined_exact}'
        if location:
            q += f' "{location}"'
        queries.append(q)

    # Each keyword separately for broader results
    for kw in all_kw:
        q = f'site:linkedin.com/in/ "{kw}"'
        if location:
            q += f' "{location}"'
        if q not in queries:
            queries.append(q)

    # Without quotes for even broader results (if single keyword has multiple words)
    for kw in all_kw:
        if " " in kw:
            q = f'site:linkedin.com/in/ {kw}'
            if location:
                q += f' {location}'
            if q not in queries:
                queries.append(q)

    # Search with ddgs for each query
    for q in queries:
        raw = _search_ddgs(q, max_results=80)
        process_results(raw, location or "")
        time.sleep(random.uniform(0.3, 0.8))

    # Fallback: Bing for each query if ddgs found nothing
    if not people:
        for q in queries:
            raw = _search_bing(q)
            process_results(raw, location or "")
            time.sleep(random.uniform(0.3, 0.8))

    return people


@app.post("/api/search/people")
def search_people(req: PeopleSearchRequest, request: Request):
    user = require_user(request)
    kw_list = req.keywords if req.keywords else ([req.keyword] if req.keyword else [])
    kw_list = [k for k in kw_list if k and k.strip()][:3]
    if not kw_list:
        return {"people": [], "count": 0}
    results = scrape_linkedin_people(kw_list, req.location)
    # Save to history
    db = SessionLocal()
    try:
        entry = PeopleSearchHistory(
            user_id=user.id,
            keywords_json=json.dumps(kw_list),
            location=req.location,
            results_json=json.dumps(results),
            result_count=len(results),
        )
        db.add(entry)
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to save search history: {e}")
    finally:
        db.close()
    return {"people": results, "count": len(results)}


@app.get("/api/search/people/history")
def get_people_history(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        entries = db.query(PeopleSearchHistory).filter(
            PeopleSearchHistory.user_id == user.id
        ).order_by(PeopleSearchHistory.created_at.desc()).limit(20).all()
        return {"history": [{
            "id": e.id,
            "keywords": json.loads(e.keywords_json),
            "location": e.location,
            "result_count": e.result_count,
            "results": json.loads(e.results_json),
            "created_at": e.created_at.isoformat() if e.created_at else "",
        } for e in entries]}
    finally:
        db.close()


@app.delete("/api/search/people/history/{entry_id}")
def delete_people_history(entry_id: int, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        entry = db.query(PeopleSearchHistory).filter(
            PeopleSearchHistory.id == entry_id,
            PeopleSearchHistory.user_id == user.id,
        ).first()
        if entry:
            db.delete(entry)
            db.commit()
        return {"ok": True}
    finally:
        db.close()


# --- Alerts ---
@app.get("/api/alerts")
def get_alerts(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        alerts = db.query(Alert).filter(Alert.user_id == user.id).order_by(Alert.created_at.desc()).limit(50).all()
        return [
            {
                "id": a.id,
                "job_title": a.job_title,
                "company": a.company,
                "url": a.url,
                "score": a.score,
                "explanation": a.explanation,
                "seen": a.seen,
                "created_at": a.created_at.isoformat() if a.created_at else "",
            }
            for a in alerts
        ]
    finally:
        db.close()


@app.post("/api/alerts/seen/{alert_id}")
def mark_alert_seen(alert_id: int, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        alert = db.query(Alert).filter(Alert.id == alert_id, Alert.user_id == user.id).first()
        if not alert:
            raise HTTPException(404, "Alerte non trouvee")
        alert.seen = 1
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


# --- Application Tracker ---
class ApplicationRequest(BaseModel):
    job_title: str = Field("", max_length=500)
    company: str = Field("", max_length=200)
    url: str = Field("", max_length=2000)
    status: str = Field("sent", max_length=50)
    notes: str = Field("", max_length=10000)


class ApplicationUpdate(BaseModel):
    status: str = Field("", max_length=50)
    notes: str = Field("", max_length=10000)


@app.post("/api/applications")
def create_application(req: ApplicationRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        app_entry = Application(
            user_id=user.id,
            job_title=req.job_title,
            company=req.company,
            url=req.url,
            status=req.status or "sent",
            notes=req.notes,
        )
        db.add(app_entry)
        db.commit()
        db.refresh(app_entry)
        return {
            "id": app_entry.id,
            "job_title": app_entry.job_title,
            "company": app_entry.company,
            "url": app_entry.url,
            "status": app_entry.status,
            "notes": app_entry.notes,
            "applied_at": app_entry.applied_at.isoformat() if app_entry.applied_at else "",
            "updated_at": app_entry.updated_at.isoformat() if app_entry.updated_at else "",
        }
    finally:
        db.close()


@app.get("/api/applications")
def list_applications(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        apps = db.query(Application).filter(Application.user_id == user.id).order_by(Application.applied_at.desc()).limit(100).all()
        return [
            {
                "id": a.id,
                "job_title": a.job_title,
                "company": a.company,
                "url": a.url,
                "status": a.status,
                "notes": a.notes,
                "applied_at": a.applied_at.isoformat() if a.applied_at else "",
                "updated_at": a.updated_at.isoformat() if a.updated_at else "",
            }
            for a in apps
        ]
    finally:
        db.close()


@app.patch("/api/applications/{app_id}")
def update_application(app_id: int, req: ApplicationUpdate, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        app_entry = db.query(Application).filter(Application.id == app_id, Application.user_id == user.id).first()
        if not app_entry:
            raise HTTPException(404, "Candidature non trouvee")
        if req.status:
            app_entry.status = req.status
        if req.notes is not None and req.notes != "":
            app_entry.notes = req.notes
        app_entry.updated_at = datetime.utcnow()
        db.commit()
        return {
            "id": app_entry.id,
            "job_title": app_entry.job_title,
            "company": app_entry.company,
            "url": app_entry.url,
            "status": app_entry.status,
            "notes": app_entry.notes,
            "applied_at": app_entry.applied_at.isoformat() if app_entry.applied_at else "",
            "updated_at": app_entry.updated_at.isoformat() if app_entry.updated_at else "",
        }
    finally:
        db.close()


@app.delete("/api/applications/{app_id}")
def delete_application(app_id: int, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        app_entry = db.query(Application).filter(Application.id == app_id, Application.user_id == user.id).first()
        if not app_entry:
            raise HTTPException(404, "Candidature non trouvee")
        db.delete(app_entry)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


# --- CV Tailoring ---
class CVTailorRequest(BaseModel):
    job_url: str = Field("", max_length=2000)
    job_description: str = Field("", max_length=10000)
    job_title: str = Field("", max_length=500)
    company: str = Field("", max_length=200)


@app.post("/api/cv/tailor")
@limiter.limit("5/minute")
def tailor_cv(req: CVTailorRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    finally:
        db.close()
    if not profile or not profile.cv:
        raise HTTPException(400, "Veuillez d'abord compléter votre profil avec votre CV.")

    job_desc = req.job_description
    if not job_desc and req.job_url:
        if not validate_url(req.job_url):
            raise HTTPException(400, "URL non autorisée")
        job_desc = fetch_job_description(req.job_url)

    cv_text = profile.cv or ""
    try:
        cvs_list = json.loads(profile.cvs_json or "[]")
        if cvs_list:
            cv_text = cvs_list[0].get("content", cv_text)
    except Exception:
        pass

    prompt = f"""Tu es un expert en optimisation de CV pour les systèmes ATS et les recruteurs.

CV actuel du candidat:
{cv_text[:3000]}

Offre d'emploi: {req.job_title} chez {req.company}
Description du poste:
{(job_desc or 'Non disponible')[:2500]}

Analyse le CV et l'offre d'emploi, puis retourne un JSON avec:
1. "tailored_cv": version optimisée du CV qui met en avant les expériences et compétences pertinentes pour ce poste (garde le contenu véridique)
2. "changes_made": liste de modifications effectuées et pourquoi (array of strings)
3. "keyword_matches": mots-clés de l'offre trouvés dans le CV (array of strings)
4. "missing_keywords": mots-clés importants de l'offre absents du CV (array of strings)

Réponds UNIQUEMENT en JSON valide."""

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=3000,
        )
        content = resp.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(content[start:end])
            return result
        raise ValueError("No JSON found")
    except json.JSONDecodeError:
        return {"tailored_cv": cv_text, "changes_made": [], "keyword_matches": [], "missing_keywords": [], "error": "Impossible de parser la réponse IA."}
    except Exception as e:
        logger.error(f"CV tailor error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")


# --- ATS Score ---
class ATSScoreRequest(BaseModel):
    job_url: str = Field("", max_length=2000)
    job_description: str = Field("", max_length=10000)


@app.post("/api/cv/ats-score")
@limiter.limit("5/minute")
def ats_score(req: ATSScoreRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    finally:
        db.close()
    if not profile or not profile.cv:
        raise HTTPException(400, "Veuillez d'abord compléter votre profil avec votre CV.")

    job_desc = req.job_description
    if not job_desc and req.job_url:
        if not validate_url(req.job_url):
            raise HTTPException(400, "URL non autorisée")
        job_desc = fetch_job_description(req.job_url)

    cv_text = profile.cv or ""
    try:
        cvs_list = json.loads(profile.cvs_json or "[]")
        if cvs_list:
            cv_text = cvs_list[0].get("content", cv_text)
    except Exception:
        pass

    prompt = f"""Tu es un système ATS (Applicant Tracking System) réaliste. Analyse ce CV par rapport à l'offre d'emploi.

CV:
{cv_text[:3000]}

Description du poste:
{(job_desc or 'Offre non disponible')[:2500]}

Évalue comme un vrai ATS et retourne un JSON avec:
1. "score": note de 0 à 100 (sois réaliste, la plupart des CV obtiennent entre 30-70)
2. "keyword_analysis": {{"found": ["mot-clé1", ...], "missing": ["mot-clé2", ...]}}
3. "format_tips": liste de conseils de formatage pour améliorer la lisibilité ATS (array of strings)
4. "improvement_suggestions": liste de suggestions concrètes d'amélioration (array of strings)

Réponds UNIQUEMENT en JSON valide."""

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=2000,
        )
        content = resp.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(content[start:end])
        raise ValueError("No JSON found")
    except json.JSONDecodeError:
        return {"score": 50, "keyword_analysis": {"found": [], "missing": []}, "format_tips": [], "improvement_suggestions": [], "error": "Impossible de parser la réponse IA."}
    except Exception as e:
        logger.error(f"ATS score error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")


# --- Interview Prep ---
class InterviewPrepRequest(BaseModel):
    job_title: str = Field("", max_length=500)
    company: str = Field("", max_length=200)
    job_description: str = Field("", max_length=10000)
    job_url: str = Field("", max_length=2000)


class InterviewSimulateRequest(BaseModel):
    job_title: str = Field("", max_length=500)
    company: str = Field("", max_length=200)
    user_answer: str = Field("", max_length=10000)
    question_index: int = 0
    conversation_history: list = []


@app.post("/api/interview/prepare")
@limiter.limit("5/minute")
def interview_prepare(req: InterviewPrepRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    finally:
        db.close()

    cv_text = ""
    if profile and profile.cv:
        cv_text = profile.cv

    job_desc = req.job_description
    if not job_desc and req.job_url:
        if not validate_url(req.job_url):
            raise HTTPException(400, "URL non autorisée")
        job_desc = fetch_job_description(req.job_url)

    prompt = f"""Tu es un coach d'entretien d'embauche expert. Prépare le candidat pour un entretien.

CV du candidat:
{cv_text[:2000]}

Poste: {req.job_title} chez {req.company}
Description: {(job_desc or 'Non disponible')[:2000]}

Génère un JSON avec:
1. "questions": tableau de 8-10 questions probables (mix de comportementales, techniques, situationnelles). Chaque question: {{"type": "behavioral"|"technical"|"situational", "question": "..."}}
2. "suggested_answers": tableau de réponses suggérées basées sur le CV du candidat (même longueur que questions). Chaque réponse: {{"question_index": 0, "answer": "..."}}
3. "company_research_tips": liste de conseils pour se renseigner sur l'entreprise (array of strings)
4. "salary_range_estimate": estimation de fourchette salariale pour ce poste (string, ex: "35 000€ - 45 000€ brut annuel")

Réponds UNIQUEMENT en JSON valide."""

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5, max_tokens=3000,
        )
        content = resp.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(content[start:end])
        raise ValueError("No JSON found")
    except json.JSONDecodeError:
        return {"questions": [], "suggested_answers": [], "company_research_tips": [], "salary_range_estimate": "", "error": "Impossible de parser la réponse IA."}
    except Exception as e:
        logger.error(f"Interview prep error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")


@app.post("/api/interview/simulate")
@limiter.limit("20/minute")
def interview_simulate(req: InterviewSimulateRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    finally:
        db.close()

    cv_text = ""
    if profile and profile.cv:
        cv_text = profile.cv[:1500]

    history_text = ""
    for h in (req.conversation_history or [])[-6:]:
        role = h.get("role", "")
        content = h.get("content", "")
        history_text += f"{role}: {content}\n"

    prompt = f"""Tu es un recruteur expérimenté conduisant un entretien pour le poste de {req.job_title} chez {req.company}.

CV du candidat: {cv_text}

Historique de la conversation:
{history_text}

Le candidat vient de répondre à la question #{req.question_index + 1}:
"{req.user_answer}"

Évalue la réponse et retourne un JSON avec:
1. "feedback": feedback constructif sur la réponse (string)
2. "score": note de 1 à 10 (number)
3. "next_question": prochaine question de suivi (string)
4. "tips": conseils d'amélioration (array of strings)

Réponds UNIQUEMENT en JSON valide."""

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5, max_tokens=1500,
        )
        content = resp.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(content[start:end])
        raise ValueError("No JSON found")
    except json.JSONDecodeError:
        return {"feedback": "", "score": 5, "next_question": "", "tips": [], "error": "Impossible de parser la réponse IA."}
    except Exception as e:
        logger.error(f"Interview simulate error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")


# --- Email System ---
class SendEmailRequest(BaseModel):
    to_email: str = Field(..., max_length=200)
    subject: str = Field(..., max_length=500)
    body: str = Field(..., max_length=50000)
    application_id: int = 0


class ScheduleFollowupRequest(BaseModel):
    application_id: int
    delay_days: int = 7
    subject: str = Field("", max_length=500)
    body: str = Field("", max_length=50000)


@app.post("/api/email/send")
@limiter.limit("10/minute")
def send_email(req: SendEmailRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
        if not profile or not profile.smtp_email or not profile.smtp_password:
            raise HTTPException(400, "Veuillez configurer vos paramètres SMTP dans votre profil.")
        try:
            _send_email(
                profile.smtp_email, profile.smtp_password,
                profile.smtp_host or "smtp.gmail.com", profile.smtp_port or 587,
                req.to_email, req.subject, req.body,
            )
        except Exception as e:
            logger.error(f"Email send error: {e}")
            raise HTTPException(500, "Erreur interne du serveur.")

        # Save to history
        eh = EmailHistory(
            user_id=user.id, to_email=req.to_email, subject=req.subject,
            body=req.body, application_id=req.application_id if req.application_id else None,
        )
        db.add(eh)

        # Update application status if provided
        if req.application_id:
            app_entry = db.query(Application).filter(
                Application.id == req.application_id, Application.user_id == user.id
            ).first()
            if app_entry:
                app_entry.status = "sent"
                app_entry.updated_at = datetime.utcnow()

        db.commit()
        return {"status": "sent"}
    finally:
        db.close()


@app.post("/api/email/schedule-followup")
def schedule_followup(req: ScheduleFollowupRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        app_entry = db.query(Application).filter(
            Application.id == req.application_id, Application.user_id == user.id
        ).first()
        if not app_entry:
            raise HTTPException(404, "Candidature non trouvée.")
        send_at = datetime.utcnow() + timedelta(days=req.delay_days)
        followup = ScheduledFollowup(
            user_id=user.id, application_id=req.application_id,
            send_at=send_at, subject=req.subject, body=req.body,
        )
        db.add(followup)
        db.commit()
        db.refresh(followup)
        return {
            "id": followup.id,
            "application_id": followup.application_id,
            "send_at": followup.send_at.isoformat(),
            "subject": followup.subject,
            "body": followup.body,
            "sent": followup.sent,
        }
    finally:
        db.close()


@app.get("/api/emails")
def get_emails(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        emails = db.query(EmailHistory).filter(
            EmailHistory.user_id == user.id
        ).order_by(EmailHistory.sent_at.desc()).limit(50).all()
        return [
            {
                "id": e.id,
                "to_email": e.to_email,
                "subject": e.subject,
                "body": e.body,
                "application_id": e.application_id,
                "sent_at": e.sent_at.isoformat() if e.sent_at else "",
            }
            for e in emails
        ]
    finally:
        db.close()


# --- Analytics ---
@app.get("/api/analytics")
def get_analytics(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        # Applications per week (last 8 weeks)
        now = datetime.utcnow()
        applications = db.query(Application).filter(Application.user_id == user.id).all()
        weeks_data = []
        for i in range(7, -1, -1):
            week_start = now - timedelta(weeks=i + 1)
            week_end = now - timedelta(weeks=i)
            count = sum(1 for a in applications if a.applied_at and week_start <= a.applied_at < week_end)
            weeks_data.append({"week": (now - timedelta(weeks=i)).strftime("%d/%m"), "count": count})

        # Response rate
        total_apps = len(applications)
        responded = sum(1 for a in applications if a.status not in ("waiting", "sent"))
        response_rate = round((responded / total_apps * 100) if total_apps > 0 else 0, 1)

        # Average score of saved jobs
        saved_jobs = db.query(SavedJob).filter(SavedJob.user_id == user.id).all()
        scores = [j.score for j in saved_jobs if j.score and j.score > 0]
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0

        # Platform breakdown
        platform_breakdown = {}
        for j in saved_jobs:
            p = j.platform or "Autre"
            platform_breakdown[p] = platform_breakdown.get(p, 0) + 1

        # Status distribution
        status_distribution = {}
        for a in applications:
            status_distribution[a.status] = status_distribution.get(a.status, 0) + 1

        # Top companies
        company_counts = {}
        for a in applications:
            if a.company:
                company_counts[a.company] = company_counts.get(a.company, 0) + 1
        top_companies = sorted(company_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_companies = [{"company": c, "count": n} for c, n in top_companies]

        # Weekly trend
        last_4 = [w["count"] for w in weeks_data[-4:]]
        if len(last_4) >= 2:
            if last_4[-1] > last_4[0]:
                weekly_trend = "improving"
            elif last_4[-1] < last_4[0]:
                weekly_trend = "declining"
            else:
                weekly_trend = "stable"
        else:
            weekly_trend = "stable"

        # AI insights
        ai_insights = []
        try:
            insight_prompt = f"""Tu es un coach carrière. Voici les statistiques de recherche d'emploi d'un candidat:
- Candidatures totales: {total_apps}
- Taux de réponse: {response_rate}%
- Score moyen des offres sauvegardées: {avg_score}/10
- Distribution des statuts: {json.dumps(status_distribution)}
- Tendance hebdomadaire: {weekly_trend}
- Top entreprises: {json.dumps([c['company'] for c in top_companies])}

Donne 2-3 conseils personnalisés et concrets en français pour améliorer sa recherche.
Réponds UNIQUEMENT en JSON: un tableau de strings. Exemple: ["conseil1", "conseil2"]"""
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": insight_prompt}],
                temperature=0.5, max_tokens=500,
            )
            content = resp.choices[0].message.content.strip()
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                ai_insights = json.loads(content[start:end])
        except Exception as e:
            logger.warning(f"Analytics AI insights error: {e}")
            ai_insights = ["Continuez vos efforts de candidature !"]

        return {
            "applications_per_week": weeks_data,
            "response_rate": response_rate,
            "avg_score": avg_score,
            "platform_breakdown": platform_breakdown,
            "status_distribution": status_distribution,
            "top_companies": top_companies,
            "weekly_trend": weekly_trend,
            "ai_insights": ai_insights,
        }
    finally:
        db.close()


# --- Networking Messages ---
class NetworkingMessageRequest(BaseModel):
    person_name: str = Field("", max_length=200)
    person_title: str = Field("", max_length=200)
    person_company: str = Field("", max_length=200)
    context: str = Field("", max_length=10000)


@app.post("/api/networking/message")
def generate_networking_message(req: NetworkingMessageRequest, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    finally:
        db.close()

    cv_text = ""
    lang = "fr"
    if profile:
        cv_text = profile.cv or ""
        lang = profile.language or "fr"

    prompt = f"""Tu es un expert en networking professionnel.

CV du candidat:
{cv_text[:1500]}

Personne à contacter:
- Nom: {req.person_name}
- Titre: {req.person_title}
- Entreprise: {req.person_company}
- Contexte: {req.context or "Aucun contexte particulier"}

Génère 3 messages d'approche différents en {"français" if lang == "fr" else "anglais"}:
1. Formel (professionnel et respectueux)
2. Décontracté (amical mais professionnel)
3. Direct (droit au but, efficace)

Chaque message doit être court (3-5 phrases), personnalisé avec les infos du CV et de la personne.

Réponds UNIQUEMENT en JSON valide:
[{{"tone": "formal", "text": "..."}}, {{"tone": "casual", "text": "..."}}, {{"tone": "direct", "text": "..."}}]"""

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7, max_tokens=1500,
        )
        content = resp.choices[0].message.content.strip()
        start = content.find("[")
        end = content.rfind("]") + 1
        if start >= 0 and end > start:
            messages = json.loads(content[start:end])
            return {"messages": messages}
        raise ValueError("No JSON found")
    except json.JSONDecodeError:
        return {"messages": [], "error": "Impossible de parser la réponse IA."}
    except Exception as e:
        logger.error(f"Networking message error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")


# --- Share Profile ---
@app.post("/api/profile/share")
def share_profile(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
        if not profile:
            raise HTTPException(404, "Profil non trouvé.")
        token = str(uuid.uuid4())
        profile.share_token = token
        db.commit()
        return {"share_url": f"/shared/{token}"}
    finally:
        db.close()


@app.get("/api/shared/{token}")
@limiter.limit("10/minute")
def get_shared_profile(token: str, request: Request):
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.share_token == token).first()
        if not profile:
            raise HTTPException(404, "Profil partagé non trouvé.")
        user = db.query(User).filter(User.id == profile.user_id).first()

        cv_text = profile.cv or ""
        try:
            cvs_list = json.loads(profile.cvs_json or "[]")
            if cvs_list:
                cv_text = cvs_list[0].get("content", cv_text)
        except Exception:
            pass

        # Completion score
        score = 0
        if profile.cv and profile.cv.strip():
            score += 30
        if profile.cover_letter and profile.cover_letter.strip():
            score += 30
        if profile.goals and profile.goals.strip():
            score += 20

        # Saved jobs titles
        saved = db.query(SavedJob).filter(SavedJob.user_id == profile.user_id).limit(100).all()
        saved_titles = [j.title for j in saved]

        # Applications summary
        apps = db.query(Application).filter(Application.user_id == profile.user_id).limit(100).all()
        apps_summary = {"total": len(apps)}
        for a in apps:
            apps_summary[a.status] = apps_summary.get(a.status, 0) + 1

        return {
            "name": user.name if user else "",
            "cv_text": cv_text[:200] if cv_text else "",
            "goals": profile.goals or "",
            "completion_score": score,
            "saved_jobs": saved_titles,
            "applications_summary": apps_summary,
        }
    finally:
        db.close()


@app.delete("/api/profile/share")
def delete_share_profile(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
        if profile:
            profile.share_token = None
            db.commit()
        return {"status": "ok"}
    finally:
        db.close()


# --- LinkedIn Import ---
class LinkedInImportRequest(BaseModel):
    linkedin_url: str = Field(..., max_length=2000)


@app.post("/api/profile/import-linkedin")
def import_linkedin(req: LinkedInImportRequest, request: Request):
    user = require_user(request)
    if not req.linkedin_url or "linkedin.com" not in req.linkedin_url:
        raise HTTPException(400, "URL LinkedIn invalide.")

    if not validate_url(req.linkedin_url):
        raise HTTPException(400, "URL non autorisée")

    resp = safe_request(req.linkedin_url)
    if not resp or resp.status_code != 200:
        raise HTTPException(400, "Impossible d'accéder à la page LinkedIn.")

    soup = BeautifulSoup(resp.content, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    # Extract key sections
    extracted_parts = []

    # Name
    name_el = soup.find("h1")
    if name_el:
        extracted_parts.append(f"Nom: {name_el.get_text(strip=True)}")

    # Headline
    headline_el = soup.find("div", class_="top-card-layout__headline") or soup.find("h2")
    if headline_el:
        extracted_parts.append(f"Titre: {headline_el.get_text(strip=True)}")

    # About / Summary
    about_section = soup.find("section", class_="summary") or soup.find("div", {"class": re.compile(r"summary|about", re.I)})
    if about_section:
        extracted_parts.append(f"À propos: {about_section.get_text(separator=' ', strip=True)[:500]}")

    # Experience
    exp_section = soup.find("section", {"class": re.compile(r"experience", re.I)})
    if exp_section:
        extracted_parts.append(f"Expérience: {exp_section.get_text(separator=' ', strip=True)[:1000]}")

    # Fallback: get body text
    if not extracted_parts:
        body = soup.find("body")
        if body:
            extracted_parts.append(body.get_text(separator="\n", strip=True)[:2000])

    extracted_text = "\n\n".join(extracted_parts)

    if not extracted_text.strip():
        raise HTTPException(400, "Impossible d'extraire des informations de cette page LinkedIn.")

    # Use Groq to format into CV
    try:
        format_resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{
                "role": "user",
                "content": f"""Voici des informations extraites d'un profil LinkedIn:

{extracted_text[:3000]}

Formate ces informations en un CV propre et structuré avec les sections:
- Informations personnelles
- Résumé professionnel
- Expérience professionnelle
- Formation
- Compétences

Réponds UNIQUEMENT avec le CV formaté, sans commentaire.""",
            }],
            temperature=0.2, max_tokens=2000,
        )
        formatted_cv = format_resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Groq LinkedIn format error: {e}")
        formatted_cv = extracted_text

    # Save as new CV version
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
        if profile:
            try:
                cvs_list = json.loads(profile.cvs_json or "[]")
            except Exception:
                cvs_list = []
            cvs_list.append({"name": "LinkedIn Import", "content": formatted_cv})
            profile.cvs_json = json.dumps(cvs_list)
            if not profile.cv or not profile.cv.strip():
                profile.cv = formatted_cv
            db.commit()
    finally:
        db.close()

    return {"extracted_text": extracted_text, "formatted_cv": formatted_cv}


# --- Export PDF ---
@app.get("/api/export/cv")
def export_cv_pdf(request: Request, version: int = 0, tailored: bool = False, job: str = ""):
    user = require_user(request)
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    finally:
        db.close()
    if not profile or not profile.cv:
        raise HTTPException(400, "Aucun CV à exporter.")

    cv_text = profile.cv or ""
    try:
        cvs_list = json.loads(profile.cvs_json or "[]")
        if cvs_list and version < len(cvs_list):
            cv_text = cvs_list[version].get("content", cv_text)
    except Exception:
        pass

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm)
        styles = getSampleStyleSheet()
        heading_style = ParagraphStyle("CVHeading", parent=styles["Heading1"], fontSize=14, spaceAfter=10)
        section_style = ParagraphStyle("CVSection", parent=styles["Heading2"], fontSize=11, spaceAfter=6, spaceBefore=12)
        body_style = ParagraphStyle("CVBody", parent=styles["Normal"], fontSize=10, spaceAfter=4, leading=14)

        story = []
        user_db = None
        db2 = SessionLocal()
        try:
            user_db = db2.query(User).filter(User.id == user.id).first()
        finally:
            db2.close()

        story.append(Paragraph(user_db.name if user_db else "CV", heading_style))
        story.append(Spacer(1, 0.3 * cm))

        # Parse CV text into sections
        lines = cv_text.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                story.append(Spacer(1, 0.2 * cm))
                continue
            # Escape XML special chars for reportlab
            safe_line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if line.startswith("#") or line.isupper() or (len(line) < 60 and line.endswith(":")):
                clean = safe_line.lstrip("#").strip().rstrip(":")
                if clean:
                    story.append(Paragraph(clean, section_style))
            else:
                story.append(Paragraph(safe_line, body_style))

        doc.build(story)
        buffer.seek(0)

        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            buffer, media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=cv.pdf"},
        )
    except ImportError:
        raise HTTPException(500, "Erreur interne du serveur.")
    except Exception as e:
        logger.error(f"PDF export error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")


@app.get("/api/export/letter/{letter_id}")
def export_letter_pdf(letter_id: int, request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        letter = db.query(GeneratedLetter).filter(
            GeneratedLetter.id == letter_id, GeneratedLetter.user_id == user.id
        ).first()
        if not letter:
            raise HTTPException(404, "Lettre non trouvée.")
        letter_content = letter.content
        letter_title = f"{letter.job_title} - {letter.company}"
    finally:
        db.close()

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=2.5 * cm, rightMargin=2.5 * cm, topMargin=2.5 * cm, bottomMargin=2.5 * cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("LetterTitle", parent=styles["Heading1"], fontSize=13, spaceAfter=12)
        body_style = ParagraphStyle("LetterBody", parent=styles["Normal"], fontSize=10, spaceAfter=6, leading=14)

        story = []
        story.append(Paragraph(letter_title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), title_style))
        story.append(Spacer(1, 0.5 * cm))

        for line in letter_content.split("\n"):
            safe_line = line.strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if not safe_line:
                story.append(Spacer(1, 0.3 * cm))
            else:
                story.append(Paragraph(safe_line, body_style))

        doc.build(story)
        buffer.seek(0)

        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            buffer, media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=lettre_{letter_id}.pdf"},
        )
    except ImportError:
        raise HTTPException(500, "Erreur interne du serveur.")
    except Exception as e:
        logger.error(f"Letter PDF export error: {e}")
        raise HTTPException(500, "Erreur interne du serveur.")


# --- Global Exception Handler ---
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled error: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Erreur interne du serveur."})
