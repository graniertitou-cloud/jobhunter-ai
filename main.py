import asyncio
import io
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import hashlib

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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel
from sqlalchemy import (
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
        conn.commit()
    logger.info("Migration check done")
except Exception as e:
    logger.warning(f"Migration check: {e}")

# --- FastAPI ---
app = FastAPI(title="JobHunter AI")
os.makedirs("static", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_current_user(request: Request) -> Optional[User]:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.session_token == token).first()
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
    return hashlib.sha256(password.encode()).hexdigest()


# --- Auth ---
class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/register")
def register(req: RegisterRequest):
    if not req.email or not req.password:
        raise HTTPException(400, "Email et mot de passe requis")
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == req.email).first()
        if existing:
            raise HTTPException(400, "Cet email est déjà utilisé")
        session_token = str(uuid.uuid4())
        user = User(
            email=req.email,
            password_hash=hash_password(req.password),
            name=req.name or req.email.split("@")[0],
            session_token=session_token,
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
def login(req: LoginRequest):
    if not req.email or not req.password:
        raise HTTPException(400, "Email et mot de passe requis")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == req.email).first()
        if not user or user.password_hash != hash_password(req.password):
            raise HTTPException(401, "Email ou mot de passe incorrect")
        session_token = str(uuid.uuid4())
        user.session_token = session_token
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


# --- Profile ---
class ProfileData(BaseModel):
    cv: str = ""
    cover_letter: str = ""
    goals: str = ""
    cvs: list = []
    cover_letters: list = []
    language: str = "fr"


@app.get("/api/profile")
def get_profile(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        p = db.query(Profile).filter(Profile.user_id == user.id).first()
        if not p:
            return {"cv": "", "cover_letter": "", "goals": "", "cvs": [], "cover_letters": [], "language": "fr"}
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
        return {
            "cv": p.cv or "",
            "cover_letter": p.cover_letter or "",
            "goals": p.goals or "",
            "cvs": cvs,
            "cover_letters": cover_letters,
            "language": p.language or "fr",
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
            )
            db.add(p)
        else:
            p.cv = main_cv
            p.cover_letter = main_cl
            p.goals = data.goals
            p.cvs_json = json.dumps(data.cvs)
            p.cover_letters_json = json.dumps(data.cover_letters)
            p.language = data.language
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
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
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
    """Scrape France Travail (ex-Pôle Emploi) — fiable, 20+ résultats."""
    jobs = []
    try:
        url = f"https://candidat.francetravail.fr/offres/recherche?motsCles={quote(keywords)}&offresPartenaires=true"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
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
        job_list_text = "\n".join(
            [f"- {j['title']} chez {j['company']} à {j['location']}" for j in batch]
        )
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
    keywords: str
    location: str = "France"
    platforms: list[str] = ["linkedin", "wttj", "francetravail"]


@app.post("/api/search")
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
    title: str
    company: str = ""
    location: str = ""
    url: str = ""
    platform: str = ""
    date: str = ""
    score: float = 0
    explanation: str = ""


@app.get("/api/jobs/saved")
def get_saved_jobs(request: Request):
    user = require_user(request)
    db = SessionLocal()
    try:
        jobs = db.query(SavedJob).filter(SavedJob.user_id == user.id).order_by(SavedJob.score.desc()).all()
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
    job_title: str = ""
    company: str = ""
    job_description: str = ""
    job_url: str = ""
    job_location: str = ""
    job_explanation: str = ""
    instruction: str = ""


def fetch_job_description(url: str) -> str:
    """Try to fetch the actual job description from the offer URL."""
    if not url or url == "#":
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

    prompt = f"""Tu es un expert en rédaction de lettres de motivation en français.

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

Rédige une lettre de motivation de ~280 mots, en français, en 3 paragraphes.
Ton professionnel mais humain, direct et confiant.
Ne commence JAMAIS par "Je me permets de vous contacter".
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
        raise HTTPException(500, "Erreur lors de la génération de la lettre.")

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
        return {"total_found": 0, "saved": saved_count, "letters": letters_count}
    finally:
        db.close()


# --- Chat ---
class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
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
        raise HTTPException(500, "Erreur de l'assistant IA.")
