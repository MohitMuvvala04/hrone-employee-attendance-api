# Employee Attendance Management API

A FastAPI and MongoDB application for managing employee records, attendance, attendance corrections, and workforce analytics.

## Features

- Employee creation, listing, and pagination.
- Attendance punch-in and punch-out.
- Attendance filtering by employee, date, and status.
- Attendance corrections with audit history.
- Employee monthly attendance analytics.
- Department-level attendance summaries.
- Late-arrival leaderboard with ranking.
- Department attendance trends and moving averages.
- MongoDB indexes and query execution statistics.

## Technology Stack

- Python
- FastAPI
- MongoDB
- PyMongo
- Pydantic

## Prerequisites

- Python 3.11 or newer.
- MongoDB 6.0 or newer.
- Dependencies listed in `requirements.txt`.

## Setup

Create and activate a virtual environment.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Configure the MongoDB connection using environment variables:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Start the API:

```powershell
python -m uvicorn app.main:app --port 8000
```

## API Documentation

With the server running, open:

- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## Main API Areas

| Area | Endpoints |
|---|---|
| Health | `GET /health` |
| Employees | `POST /employees`, `GET /employees` |
| Attendance | `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance` |
| Attendance corrections | `PATCH /attendance/{emp_code}/{date}` |
| Employee analytics | `GET /analytics/employees/{emp_code}/monthly` |
| Department analytics | `GET /analytics/departments/summary` |
| Late-arrival leaderboard | `GET /analytics/leaderboard/late` |
| Department trends | `GET /analytics/departments/{department}/trend` |
| Query explanations | `GET /admin/explain/{endpoint}` |

## Project Structure

```text
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── .gitignore
├── DATA_MODEL.md
├── DECISIONS.md
├── README.md
├── REVIEW.md
├── openapi.yaml
├── requirements.txt
└── sample_seed.py
```

## Notes

- MongoDB connection settings are supplied through environment variables.
- Do not commit `.env` files, credentials, or other secrets.
- `sample_seed.py` replaces the documents in the sample collections. Use it only with a disposable test database.
- All implementation code for the assignment is in `app/main.py`.