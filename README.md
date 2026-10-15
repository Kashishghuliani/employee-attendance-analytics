# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.


# Employee Attendance & Analytics API

A REST API built with **Python, FastAPI, and MongoDB** to manage employee information, track daily attendance, handle attendance corrections, and generate attendance analytics.

## About the Project

The Employee Attendance & Analytics API provides a backend system for managing employee attendance records and analyzing attendance patterns.

The application allows clients to create and retrieve employee records, record punch-in and punch-out times, correct attendance records through regularization, and retrieve analytics such as monthly attendance summaries, department-level statistics, and late-arrival leaderboards.

The project focuses on reliable data storage, accurate attendance calculations, consistent API responses, and efficient MongoDB queries.

## Technology Stack

| Technology | Purpose |
|---|---|
| Python | Application logic and calculations |
| FastAPI | Building REST API endpoints |
| PyMongo | Connecting to and querying MongoDB |
| MongoDB 6.0+ | Storing employee and attendance records |
| Uvicorn | Running the API server |

## Key Features

- **Employee Management:** Create and retrieve employee information.
- **Attendance Tracking:** Record employee punch-in and punch-out events.
- **Attendance Regularization:** Correct attendance records while maintaining an audit history.
- **Attendance Calculations:** Calculate work hours, late minutes, overtime, and half-day status according to the assignment rules.
- **Employee Analytics:** Retrieve attendance statistics for individual employees.
- **Department Analytics:** Generate department-level attendance summaries and trends.
- **Late-Arrival Leaderboard:** Rank employees using their stored late-minute totals.
- **Database Integrity:** Use MongoDB indexes to enforce uniqueness and support common queries.
- **Health and Query-Plan Checks:** Check database connectivity and inspect query execution plans.

## Project Structure

```text
candidate_kit/
├── app/
│   └── main.py
├── sample_data/
├── .env.example
├── .gitignore
├── requirements.txt
├── sample_seed.py
├── openapi.yaml
├── DATA_MODEL.md
├── REVIEW.md
├── DECISIONS.md
└── README.md
```

## Prerequisites

Before running the project, make sure you have:

- Python installed with a version compatible with the project dependencies.
- MongoDB 6.0 or newer, either locally or through MongoDB Atlas.
- Git, if you are cloning the repository.

## Installation and Setup

### 1. Clone the repository

```bash
git clone YOUR_PUBLIC_GITHUB_REPOSITORY_URL
cd candidate_kit
```

Replace the placeholder URL with your actual public GitHub repository URL.

### 2. Create and activate a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux or macOS:**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure MongoDB

Create a `.env` file in the project root using `.env.example` as a reference.

Add your MongoDB configuration:

```dotenv
MONGO_URI=your_mongodb_connection_string
MONGO_DB=your_database_name
```

Replace these example values with your own database connection details. Keep your `.env` file private and never commit database credentials to GitHub.

### 5. Load the sample data (optional)

The repository includes sample attendance documents for local testing.

To load them into the configured MongoDB database, run:

```bash
python sample_seed.py
```

The supplied samples demonstrate the expected document structure. The implementation must also support other valid datasets that follow the assignment's data model and business rules.

### 6. Start the API server

Run the following command from the project root:

```bash
uvicorn app.main:app --port 8000
```

For development, you can enable automatic reloading:

```bash
uvicorn app.main:app --port 8000 --reload
```

## API Documentation

Once the server is running, open either of these URLs in your browser:

- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

Swagger UI lets you explore available endpoints, enter request parameters, send API requests, and inspect responses.

## Database Design

The application uses two MongoDB collections.

**`employees`**

Stores employee information, including employee code, name, email, department, shift timings, joining date, and creation timestamp.

**`attendance_logs`**

Stores daily attendance records, including status, punch-in and punch-out timestamps, calculated attendance values, and regularization history.

The implementation follows the supplied `DATA_MODEL.md` and `openapi.yaml`. Employee codes are used as business identifiers, timestamps are converted at the API boundary, and indexes are created by the application to support data integrity and query performance.

## Testing and Validation

The API can be tested through Swagger UI or an HTTP client.

Important scenarios to validate include:

- Creating employees and handling duplicate employee codes.
- Recording punch-in and punch-out events.
- Handling duplicate or simultaneous punch-in requests.
- Correctly processing overnight shifts and timestamp conversions.
- Regularizing attendance and preserving correction history.
- Handling employees who have no attendance records.
- Checking monthly analytics, department summaries, and leaderboard ties.
- Verifying database health and query execution plans.

The supplied sample data is useful for initial checks, but it does not cover every edge case that may be evaluated.

## Supporting Documentation

| File | Description |
|---|---|
| `openapi.yaml` | Defines API endpoints, request and response formats, and business rules. |
| `DATA_MODEL.md` | Documents MongoDB collections, fields, data types, and sample documents. |
| `REVIEW.md` | Records defects identified in the starter code, how to reproduce them, and their fixes. |
| `DECISIONS.md` | Explains implementation choices, concurrency handling, index design, and scaling considerations. |

## Security and Configuration

- Database credentials are provided through environment variables.
- The `.env` file must not be committed to the repository.
- MongoDB connection details and other secrets must not be included in source code or documentation.

## Conclusion

This project demonstrates backend API development with FastAPI and MongoDB, attendance business logic, database indexing, concurrency handling, data validation, and analytics using MongoDB queries and aggregation pipelines.