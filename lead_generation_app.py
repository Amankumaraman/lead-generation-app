import csv
import io
import json
import logging
import os
import queue
import random
import re
import time
from datetime import datetime
from threading import Thread
from typing import Dict, Generator, List, Optional
from urllib.parse import urljoin

# Flask & Extensions
from flask import Flask, request, Response, jsonify, send_file, render_template
from flask_cors import CORS

# Third-party Libraries
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Define headers for CSV and Google Sheets
HEADERS = [
    "Name", "Firm", "Email", "Website", "Source", "State", "Timestamp",
    "Name Verified", "Firm Verified", "Email Verified", "Website Verified", "Confidence Score"
]

# Cache file for attorneys
CACHE_FILE = "attorneys.json"

# ==================== CONFIGURATION ====================
class Config:
    def __init__(self, states=None, practice_area=None):
        self.STATES = states or json.loads(os.getenv("STATES", '["California"]'))
        self.PRACTICE_AREA = practice_area or os.getenv("PRACTICE_AREA", "Personal Injury")
        self.GOOGLE_CREDENTIALS = os.path.join(os.path.dirname(__file__), "credentials.json")
        self.SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
        self.WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Leads")
        self.API_HOST = os.getenv("API_HOST", "0.0.0.0")
        self.API_PORT = int(os.getenv("API_PORT", 5000))
        self.API_DEBUG = os.getenv("API_DEBUG", "True").lower() == "true"
        self.CORS_ORIGINS = json.loads(os.getenv("CORS_ORIGINS", '["http://localhost:5000"]'))
        self.HEADLESS = os.getenv("HEADLESS", "True").lower() == "true"
        self.TIMEOUT = int(os.getenv("TIMEOUT", 60))
        self.MAX_RESULTS_PER_STATE = int(os.getenv("MAX_RESULTS_PER_STATE", 10))
        self.REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", 3))
        self.STREAMING_DELAY = float(os.getenv("STREAMING_DELAY", 0.5))
        self.USER_AGENTS = json.loads(os.getenv(
            "USER_AGENTS",
            '["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"]'
        ))
        self.SOURCES = {
            "justia": {
                "url": "https://www.justia.com/lawyers/{practice_area}/{state}",
                "requires_js": False,
                "selectors": {
                    "profiles": ".lawyer-card",
                    "name": ".lawyer-name",
                    "firm": ".lawyer-firm",
                    "website": ".lawyer-website a"
                }
            }
        }

    def get_source_url(self, source_name: str, state: str) -> Optional[str]:
        source = self.SOURCES.get(source_name)
        if not source:
            return None
        return source["url"].format(
            practice_area=self.PRACTICE_AREA.lower().replace(" ", "-"),
            state=state.lower().replace(" ", "-")
        )

# ==================== UTILS ====================
def clean_text(text: str) -> str:
    return " ".join(text.strip().split()) if text else ""

def validate_email(email: str) -> bool:
    if not email:
        return False
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))

def save_attorneys(attorneys: List[Dict]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(attorneys, f)
        logger.info(f"Saved {len(attorneys)} attorneys to {CACHE_FILE}")
    except Exception as e:
        logger.error(f"Error saving attorneys: {e}")

def load_attorneys() -> List[Dict]:
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return []
    except Exception as e:
        logger.error(f"Error loading attorneys: {e}")
        return []

# ==================== SCRAPER ====================
class AttorneyScraper:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.driver = None
        self._init_session()

    def _init_session(self):
        self.session.headers.update({
            "User-Agent": random.choice(self.config.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        })

    def scrape_sources(self) -> List[Dict]:
        attorneys = []
        for state in self.config.STATES:
            results = self._scrape_justia(state)
            attorneys.extend([a for a in results if a not in attorneys])
            time.sleep(self.config.REQUEST_DELAY)
            state_attorneys = [a for a in attorneys if a["state"] == state]
            if len(state_attorneys) > self.config.MAX_RESULTS_PER_STATE:
                attorneys = (
                    [a for a in attorneys if a["state"] != state] +
                    state_attorneys[:self.config.MAX_RESULTS_PER_STATE]
                )
        logger.info(f"Scraped {len(attorneys)} attorneys")
        return attorneys

    def _scrape_justia(self, state: str) -> List[Dict]:
        url = self.config.get_source_url("justia", state)
        if not url:
            logger.error(f"Invalid Justia URL for {state}")
            return []

        try:
            response = self.session.get(url, timeout=self.config.TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            profiles = soup.select(self.config.SOURCES["justia"]["selectors"]["profiles"])
            if not profiles:
                logger.warning(f"No profiles found for {state}")
                return []

            attorneys = []
            for profile in profiles[:self.config.MAX_RESULTS_PER_STATE]:
                name_elem = profile.select_one(self.config.SOURCES["justia"]["selectors"]["name"])
                firm_elem = profile.select_one(self.config.SOURCES["justia"]["selectors"]["firm"])
                website_elem = profile.select_one(self.config.SOURCES["justia"]["selectors"]["website"])

                name = clean_text(name_elem.text) if name_elem else ""
                if not name:
                    continue

                attorneys.append({
                    "name": name,
                    "firm": clean_text(firm_elem.text) if firm_elem else "",
                    "email": "",
                    "website": website_elem["href"] if website_elem else "",
                    "source": "justia",
                    "state": state,
                    "timestamp": datetime.now().isoformat()
                })
            return attorneys
        except Exception as e:
            logger.error(f"Justia scraping error for {state}: {e}")
            return []

    def close(self):
        self.session.close()

# ==================== VERIFIER ====================
class AttorneyVerifier:
    def __init__(self, config: Config):
        self.config = config

    def verify_attorney(self, attorney: Dict) -> Dict:
        verified = attorney.copy()
        verified.update({
            "name_verified": bool(verified.get("name", "") and len(verified["name"].strip()) >= 3),
            "firm_verified": bool(verified.get("firm", "") and any(c.isalpha() for c in verified["firm"])),
            "email_verified": validate_email(verified.get("email", "")),
            "website_verified": self._verify_website(verified.get("website", "")),
            "confidence_score": 0.0
        })
        verified["confidence_score"] = self._calculate_confidence_score(verified)
        return verified

    def _verify_website(self, website: str) -> bool:
        if not website:
            return False
        try:
            response = requests.head(website, timeout=10, allow_redirects=True)
            return response.status_code == 200
        except Exception:
            return False

    def _calculate_confidence_score(self, attorney: Dict) -> float:
        score = sum([
            0.2 if attorney.get("name_verified") else 0,
            0.2 if attorney.get("firm_verified") else 0,
            0.3 if attorney.get("email_verified") else 0,
            0.3 if attorney.get("website_verified") else 0
        ])
        return score / 1.0 if score > 0 else 0.0

# ==================== SHEETS WRITER ====================
class GoogleSheetsWriter:
    def __init__(self, config: Config):
        self.config = config
        self.client = None
        self.sheet = None
        self._init_client()

    def _init_client(self):
        try:
            if not os.path.exists(self.config.GOOGLE_CREDENTIALS):
                logger.error("Google Sheets credentials file missing")
                return
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_name(self.config.GOOGLE_CREDENTIALS, scope)
            self.client = gspread.authorize(creds)
            self._init_sheet()
        except Exception as e:
            logger.error(f"Google Sheets initialization failed: {e}")

    def _init_sheet(self):
        try:
            spreadsheet = self.client.open_by_key(self.config.SPREADSHEET_ID)
            try:
                self.sheet = spreadsheet.worksheet(self.config.WORKSHEET_NAME)
            except gspread.WorksheetNotFound:
                self.sheet = spreadsheet.add_worksheet(self.config.WORKSHEET_NAME, rows=1000, cols=len(HEADERS))
                self.sheet.append_row(HEADERS)
        except Exception as e:
            logger.error(f"Failed to initialize sheet: {e}")

    def write_attorneys(self, attorneys: List[Dict]):
        if not self.sheet:
            logger.error("Google Sheets not initialized")
            return
        rows = [[
            a.get("name", ""),
            a.get("firm", ""),
            a.get("email", ""),
            a.get("website", ""),
            a.get("source", ""),
            a.get("state", ""),
            a.get("timestamp", ""),
            str(a.get("name_verified", False)),
            str(a.get("firm_verified", False)),
            str(a.get("email_verified", False)),
            str(a.get("website_verified", False)),
            str(a.get("confidence_score", 0.0))
        ] for a in attorneys if a.get("name")]
        if rows:
            self.sheet.append_rows(rows, value_input_option="USER_ENTERED")

    def get_spreadsheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.config.SPREADSHEET_ID}" if self.config.SPREADSHEET_ID else ""

    def save_to_csv(self, attorneys: List[Dict], filename: str) -> bool:
        try:
            with open(filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(HEADERS)
                for a in attorneys:
                    if a.get("name"):
                        writer.writerow([
                            a.get("name", ""),
                            a.get("firm", ""),
                            a.get("email", ""),
                            a.get("website", ""),
                            a.get("source", ""),
                            a.get("state", ""),
                            a.get("timestamp", ""),
                            str(a.get("name_verified", False)),
                            str(a.get("firm_verified", False)),
                            str(a.get("email_verified", False)),
                            str(a.get("website_verified", False)),
                            str(a.get("confidence_score", 0.0))
                        ])
            return True
        except Exception as e:
            logger.error(f"CSV write error: {e}")
            return False

# ==================== LEAD GENERATION ====================
class LeadGenerationProgress:
    def __init__(self, config: Config):
        self.progress_queue = queue.Queue()
        self.config = config

    def update_progress(self, percentage: int, message: str) -> None:
        self.progress_queue.put({"progress": {"percentage": percentage, "message": message}})

    def add_result(self, attorney: Dict) -> None:
        self.progress_queue.put({"result": attorney})

    def stream(self) -> Generator[str, None, None]:
        while True:
            try:
                data = self.progress_queue.get(timeout=180)
                if data == "DONE":
                    yield f"data: {json.dumps({'status': 'complete'})}\n\n"
                    break
                yield f"data: {json.dumps(data)}\n\n"
                time.sleep(self.config.STREAMING_DELAY)
            except queue.Empty:
                yield f"data: {json.dumps({'error': 'Stream timeout'})}\n\n"
                break
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break

class LeadGenerationAgent:
    def __init__(self, config: Config, progress: LeadGenerationProgress):
        self.config = config
        self.progress = progress
        self.scraper = AttorneyScraper(config)
        self.verifier = AttorneyVerifier(config)
        self.writer = GoogleSheetsWriter(config)
        self.attorneys = []

    def run(self) -> bool:
        self.progress.update_progress(0, "Starting lead generation...")
        try:
            self.attorneys = self.scraper.scrape_sources()
            self.attorneys.append({
                "name": "Test Attorney",
                "firm": "Test Firm",
                "email": "test@example.com",
                "website": "https://example.com",
                "source": "test",
                "state": "California",
                "timestamp": datetime.now().isoformat()
            })
            save_attorneys(self.attorneys)

            self.progress.update_progress(30, f"Found {len(self.attorneys)} attorneys")
            if not self.attorneys:
                self.progress.update_progress(0, "No attorneys found")
                return False

            verified_attorneys = []
            total = len(self.attorneys)
            for i, attorney in enumerate(self.attorneys):
                self.progress.update_progress(
                    30 + int((i / total) * 50),
                    f"Verifying attorney {i + 1}/{total}: {attorney.get('name', '')}"
                )
                verified = self.verifier.verify_attorney(attorney)
                verified_attorneys.append(verified)
                self.progress.add_result(verified)

            self.progress.update_progress(80, "Writing to Google Sheets...")
            self.writer.write_attorneys(verified_attorneys)

            self.progress.update_progress(90, "Saving to CSV...")
            csv_filename = f"attorney_leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            self.writer.save_to_csv(verified_attorneys, csv_filename)

            self.progress.update_progress(100, "Lead generation complete")
            self.progress.progress_queue.put("DONE")
            return True
        except Exception as e:
            self.progress.update_progress(0, f"Error: {str(e)}")
            logger.error(f"Lead generation error: {e}")
            return False
        finally:
            self.scraper.close()

# ==================== FLASK APP ====================
app = Flask(__name__, template_folder=".", static_folder="static")
CORS(app, origins=Config().CORS_ORIGINS)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/search")
def search():
    try:
        states = json.loads(request.args.get("states", "[]"))
        practice_area = request.args.get("practice_area", "")
        if not states or not practice_area:
            return jsonify({"error": "States and practice area are required"}), 400

        config = Config(states=states, practice_area=practice_area)
        progress = LeadGenerationProgress(config)
        agent = LeadGenerationAgent(config, progress)

        Thread(target=agent.run).start()
        return Response(progress.stream(), mimetype="text/event-stream")
    except Exception as e:
        logger.error(f"Search error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/export/csv")
def export_csv():
    try:
        attorneys = load_attorneys()
        if not attorneys:
            return jsonify({"error": "No data available"}), 400

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(HEADERS)
        for a in sorted(attorneys, key=lambda x: x.get("name", "")):
            writer.writerow([
                a.get("name", ""),
                a.get("firm", ""),
                a.get("email", ""),
                a.get("website", ""),
                a.get("source", ""),
                a.get("state", ""),
                a.get("timestamp", ""),
                str(a.get("name_verified", False)),
                str(a.get("firm_verified", False)),
                str(a.get("email_verified", False)),
                str(a.get("website_verified", False)),
                str(a.get("confidence_score", 0.0))
            ])

        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"attorney_leads_{datetime.now().strftime('%Y%m%d')}.csv"
        )
    except Exception as e:
        logger.error(f"CSV export error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/export/sheets")
def export_sheets():
    try:
        attorneys = load_attorneys()
        if not attorneys:
            return jsonify({"error": "No data available"}), 400

        config = Config()
        verifier = AttorneyVerifier(config)
        writer = GoogleSheetsWriter(config)
        verified_attorneys = [verifier.verify_attorney(a) for a in attorneys]
        writer.write_attorneys(verified_attorneys)
        url = writer.get_spreadsheet_url()
        return jsonify({"success": True, "url": url})
    except Exception as e:
        logger.error(f"Google Sheets export error: {e}")
        return jsonify({"error": str(e)}), 500

# ==================== HTML TEMPLATE ====================
html_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Attorney Lead Generator</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.2/font/bootstrap-icons.min.css">
    <style>
        body { background-color: #f8f9fa; padding-top: 20px; }
        .result-item { border-left: 4px solid #007bff; padding: 10px; margin-bottom: 10px; background-color: #fff; border-radius: 5px; }
        .confidence-high { background-color: #28a745; color: #fff; }
        .confidence-medium { background-color: #fd7e14; color: #fff; }
        .confidence-low { background-color: #dc3545; color: #fff; }
        #resultsContainer { max-height: 300px; overflow-y: auto; }
        .error-message { color: #dc3545; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <div class="row justify-content-center">
            <div class="col-md-10">
                <div class="card mb-4">
                    <div class="card-header bg-primary text-white">
                        <h5 class="mb-0"><i class="bi bi-person-lines-fill"></i> Attorney Lead Generator</h5>
                    </div>
                    <div class="card-body">
                        <form id="searchForm">
                            <div class="row mb-3">
                                <div class="col-md-6">
                                    <label for="practiceArea" class="form-label">Practice Area</label>
                                    <input type="text" class="form-control" id="practiceArea" required placeholder="e.g., Personal Injury">
                                </div>
                                <div class="col-md-6">
                                    <label for="states" class="form-label">States</label>
                                    <select class="form-select" multiple id="states" required>
                                        <option value="California">California</option>
                                        <option value="New York">New York</option>
                                        <option value="Texas">Texas</option>
                                    </select>
                                    <small class="text-muted">Hold Ctrl/Cmd to select multiple states</small>
                                </div>
                            </div>
                            <div class="d-grid gap-2 d-md-flex justify-content-md-end">
                                <button type="submit" class="btn btn-primary" id="searchBtn">
                                    <i class="bi bi-search"></i> Generate Leads
                                </button>
                                <button type="button" class="btn btn-success" id="exportCsvBtn" disabled>
                                    <i class="bi bi-file-earmark-excel"></i> Export CSV
                                </button>
                                <button type="button" class="btn btn-warning" id="exportSheetsBtn" disabled>
                                    <i class="bi bi-google"></i> Export to Google Sheets
                                </button>
                            </div>
                        </form>
                    </div>
                </div>
                <div class="card mb-4">
                    <div class="card-header bg-info text-white">
                        <h5 class="mb-0"><i class="bi bi-graph-up"></i> Progress</h5>
                    </div>
                    <div class="card-body">
                        <div class="progress">
                            <div id="progressBar" class="progress-bar progress-bar-striped progress-bar-animated"
                                 role="progressbar" style="width: 0%">0%</div>
                        </div>
                        <div id="progressMessage" class="text-muted">Ready to start...</div>
                        <div id="errorMessage" class="error-message" style="display: none;"></div>
                    </div>
                </div>
                <div class="card">
                    <div class="card-header bg-secondary text-white">
                        <h5 class="mb-0"><i class="bi bi-list-ul"></i> Results
                            <span id="resultCount" class="badge bg-light text-dark">0</span></h5>
                    </div>
                    <div class="card-body">
                        <div id="resultsContainer">
                            <div class="text-center text-muted" id="noResultsMessage">
                                No results yet. Start a search to see attorney leads.
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        document.addEventListener("DOMContentLoaded", () => {
            const searchForm = document.getElementById("searchForm");
            const searchBtn = document.getElementById("searchBtn");
            const exportCsvBtn = document.getElementById("exportCsvBtn");
            const exportSheetsBtn = document.getElementById("exportSheetsBtn");
            const progressBar = document.getElementById("progressBar");
            const progressMessage = document.getElementById("progressMessage");
            const errorMessage = document.getElementById("errorMessage");
            const resultsContainer = document.getElementById("resultsContainer");
            const noResultsMessage = document.getElementById("noResultsMessage");
            const resultCount = document.getElementById("resultCount");

            let eventSource = null;
            let leads = [];

            searchForm.addEventListener("submit", (e) => {
                e.preventDefault();
                const practiceArea = document.getElementById("practiceArea").value.trim();
                const states = Array.from(document.getElementById("states").selectedOptions).map(opt => opt.value);

                if (!practiceArea || !states.length) {
                    errorMessage.style.display = "block";
                    errorMessage.textContent = "Please enter a practice area and select at least one state";
                    return;
                }

                leads = [];
                resultsContainer.innerHTML = "";
                noResultsMessage.style.display = "block";
                resultCount.textContent = "0";
                progressBar.style.width = "0%";
                progressBar.textContent = "0%";
                progressBar.classList.add("progress-bar-animated", "progress-bar-striped");
                progressBar.classList.remove("bg-success", "bg-danger");
                progressMessage.textContent = "Starting search...";
                errorMessage.style.display = "none";
                searchBtn.disabled = true;
                searchBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Searching...';
                exportCsvBtn.disabled = true;
                exportSheetsBtn.disabled = true;

                if (eventSource) eventSource.close();

                const url = `/api/search?states=${encodeURIComponent(JSON.stringify(states))}&practice_area=${encodeURIComponent(practiceArea)}`;
                eventSource = new EventSource(url);

                eventSource.onmessage = (e) => {
                    try {
                        const data = JSON.parse(e.data);
                        if (data.status === "complete") {
                            progressBar.classList.remove("progress-bar-animated", "progress-bar-striped");
                            progressBar.classList.add("bg-success");
                            progressBar.style.width = "100%";
                            progressBar.textContent = "100%";
                            progressMessage.textContent = "Search complete";
                            searchBtn.disabled = false;
                            searchBtn.innerHTML = '<i class="bi bi-search"></i> Generate Leads';
                            exportCsvBtn.disabled = !leads.length;
                            exportSheetsBtn.disabled = !leads.length;
                            if (!leads.length) {
                                errorMessage.style.display = "block";
                                errorMessage.textContent = "No attorneys found.";
                            }
                            eventSource.close();
                            return;
                        }

                        if (data.error) throw new Error(data.error);

                        if (data.progress) {
                            const percentage = Math.min(data.progress.percentage, 100);
                            progressBar.style.width = `${percentage}%`;
                            progressBar.textContent = `${percentage}%`;
                            progressMessage.textContent = data.progress.message;
                        }

                        if (data.result) {
                            noResultsMessage.style.display = "none";
                            leads.push(data.result);
                            resultCount.textContent = leads.length;
                            addResultToUI(data.result);
                        }
                    } catch (err) {
                        errorMessage.style.display = "block";
                        errorMessage.textContent = `Error: ${err.message}`;
                    }
                };

                eventSource.onerror = (e) => {
                    progressBar.classList.remove("progress-bar-animated", "progress-bar-striped");
                    progressBar.classList.add("bg-danger");
                    progressBar.style.width = "100%";
                    progressBar.textContent = "Error";
                    progressMessage.textContent = "Connection lost";
                    errorMessage.style.display = "block";
                    errorMessage.textContent = "Search failed. Please try again.";
                    searchBtn.disabled = false;
                    searchBtn.innerHTML = '<i class="bi bi-search"></i> Generate Leads';
                    exportCsvBtn.disabled = !leads.length;
                    exportSheetsBtn.disabled = !leads.length;
                    if (eventSource) eventSource.close();
                };
            });

            function addResultToUI(attender) {
                const confidence = attender.confidence_score || 0;
                const confidenceClass = confidence > 0.7 ? "confidence-high" : confidence > 0.4 ? "confidence-medium" : "confidence-low";

                const div = document.createElement("div");
                div.className = "result-item";
                div.innerHTML = `
                    <div class="d-flex justify-content-between">
                        <div>
                            <h5>${escapeHtml(attender.name || "N/A")}</h5>
                            <p class="mb-1"><strong>Firm:</strong> ${escapeHtml(attender.firm || "N/A")}</p>
                            <p class="mb-1"><strong>Email:</strong> ${escapeHtml(attender.email || "N/A")}</p>
                            <p class="mb-1"><strong>Website:</strong> ${
                                attender.website ? `<a href="${escapeHtml(attender.website)}" target="_blank">${escapeHtml(attender.website)}</a>` : "N/A"
                            }</p>
                            <p class="mb-0"><strong>State:</strong> ${escapeHtml(attender.state || "N/A")}</p>
                        </div>
                        <div class="text-end">
                            <span class="badge ${confidenceClass}">Score: ${Math.round(confidence * 100)}%</span>
                            <p class="text-muted mt-2 mb-0"><small>${escapeHtml(attender.source || "N/A")}</small></p>
                        </div>
                    </div>
                `;
                resultsContainer.prepend(div);
            }

            function escapeHtml(text) {
                const map = {
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&apos;'
                };
                return text ? String(text).replace(/[&]/g, m => map[m]) : text;
            }

            exportCsvBtn.addEventListener("click", () => {
                window.location.href = "/api/export/csv";
            });

            exportSheetsBtn.addEventListener("click", () => {
                exportSheetsBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Exporting...';
                exportSheetsBtn.disabled = true;
                fetch("/api/export/sheets")
                    .then(res => res.json())
                    .then(data => {
                        if (data.success) {
                            alert(`Exported to Google Sheets: ${data.url}`);
                            window.open(data.url, "_blank");
                        } else {
                            alert(`Error: ${data.error}`);
                        }
                    })
                    .catch(err => alert(`Error: ${err.message}`))
                    .finally(() => {
                        exportSheetsBtn.innerHTML = '<i class="bi bi-google"></i> Export to Google Sheets';
                        exportSheetsBtn.disabled = !leads.length;
                    });
            });
        });
    </script>
</body>
</html>
"""

# Write index.html
with open("index.html", "w", encoding="utf-8") as f:
    f.write(html_template)

if not os.path.exists("static"):
    os.makedirs("static")

if __name__ == "__main__":
    config = Config()
    app.run(host=config.API_HOST, port=config.API_PORT, debug=config.API_DEBUG)
