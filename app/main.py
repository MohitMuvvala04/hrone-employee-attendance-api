"""
Employee Attendance & Analytics API
HROne Software Engineer Assignment

Run:
    uvicorn app.main:app --port 8000

Configuration:
    MONGO_URI
    MONGO_DB
"""

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from pymongo.command_cursor import CommandCursor
from bson import json_util


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

employees = db["employees"]
attendance = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# Database indexes
# ---------------------------------------------------------------------------

@app.on_event("startup")
def create_indexes():
    client.admin.command("ping")

    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined",
    )

    attendance.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    attendance.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    attendance.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_punch_lookup",
    )
    attendance.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_date_status",
    )


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def fail(status_code: int, message: str):
    raise HTTPException(status_code=status_code, detail=message)


def round_half_up(value, places=2):
    if value is None:
        return None

    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def epoch_ms(value: Optional[datetime]):
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    value = truncate_seconds(value)
    return int(value.timestamp() * 1000)


def instant_from_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        fail(422, "Timestamp must be an integer in epoch milliseconds")

    if value < 100_000_000_000 or value > 4_102_444_800_000:
        fail(422, "Timestamp is outside the allowed epoch-millisecond range")

    try:
        result = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        fail(422, "Invalid timestamp")

    return truncate_seconds(result)


def now_utc():
    return truncate_seconds(datetime.now(UTC))


def to_ist(value: datetime):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        fail(422, "Date must use YYYY-MM-DD format")

    if parsed.isoformat() != value:
        fail(422, "Date must use YYYY-MM-DD format")

    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        fail(422, "Month must use YYYY-MM format")

    year, month = map(int, value.split("-"))
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])

    return start, end


def parse_shift_time(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(
        r"([01]\d|2[0-3]):[0-5]\d",
        value,
    ):
        fail(422, "Shift time must use HH:MM format")

    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def ist_datetime(day: date, shift_time: str) -> datetime:
    return datetime.combine(
        day,
        parse_shift_time(shift_time),
        tzinfo=IST,
    )


def is_working_day(day: date) -> bool:
    return day.weekday() < 5


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0

    count = 0
    current = start

    while current <= end:
        if is_working_day(current):
            count += 1
        current += timedelta(days=1)

    return count


def serialize_employee(document: dict) -> dict:
    document = dict(document)
    document.pop("_id", None)

    if isinstance(document.get("created_at"), datetime):
        document["created_at"] = epoch_ms(document["created_at"])

    return document


def serialize_attendance(document: dict) -> dict:
    document = dict(document)
    document.pop("_id", None)

    document.setdefault("punch_in", None)
    document.setdefault("punch_out", None)
    document.setdefault("work_hours", None)
    document.setdefault("late_minutes", 0)
    document.setdefault("overtime_minutes", 0)
    document.setdefault("half_day", False)
    document.setdefault("history", [])

    document["punch_in"] = epoch_ms(document.get("punch_in"))
    document["punch_out"] = epoch_ms(document.get("punch_out"))

    history = []

    for entry in document.get("history", []):
        entry = dict(entry)

        if isinstance(entry.get("at"), datetime):
            entry["at"] = epoch_ms(entry["at"])

        changes = entry.get("changes", {})

        for field in ("punch_in", "punch_out"):
            if field in changes:
                for direction in ("from", "to"):
                    value = changes[field].get(direction)

                    if isinstance(value, datetime):
                        changes[field][direction] = epoch_ms(value)

        entry["changes"] = changes
        history.append(entry)

    document["history"] = history

    return document


def validate_pagination(page: int, page_size: int):
    if page < 1:
        fail(422, "page must be at least 1")

    if page_size < 1 or page_size > 100:
        fail(422, "page_size must be between 1 and 100")


def attendance_date(punch_in: datetime, employee: dict) -> date:
    local = to_ist(punch_in)
    shift_start = parse_shift_time(employee["shift_start"])
    shift_end = parse_shift_time(employee["shift_end"])

    overnight = shift_end <= shift_start

    day = local.date()

    if overnight and local.time().replace(tzinfo=None) < shift_end:
        day -= timedelta(days=1)

    return day


def shift_start_datetime(day: date, employee: dict) -> datetime:
    return ist_datetime(day, employee["shift_start"])


def shift_end_datetime(day: date, employee: dict) -> datetime:
    start_time = parse_shift_time(employee["shift_start"])
    end_time = parse_shift_time(employee["shift_end"])

    end_day = day

    if end_time <= start_time:
        end_day += timedelta(days=1)

    return ist_datetime(end_day, employee["shift_end"])


def compute_late_minutes(punch_in: datetime, employee: dict, day: date) -> int:
    actual = to_ist(punch_in)
    expected = shift_start_datetime(day, employee)

    elapsed = int((actual - expected).total_seconds())

    if elapsed <= 600:
        return 0

    return max(0, elapsed // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()

    return round_half_up(seconds / 3600, 2)


def compute_overtime(
    punch_out: datetime,
    employee: dict,
    day: date,
) -> int:
    expected_end = shift_end_datetime(day, employee)
    elapsed_seconds = int(
        (to_ist(punch_out) - expected_end).total_seconds()
    )

    minutes = elapsed_seconds // 60

    return minutes if minutes >= 30 else 0


def compute_derived_fields(document: dict, employee: dict) -> dict:
    document = dict(document)

    document.setdefault("punch_in", None)
    document.setdefault("punch_out", None)

    if document["status"] not in PRESENT_STATUSES:
        document["punch_in"] = None
        document["punch_out"] = None
        document["work_hours"] = None
        document["late_minutes"] = 0
        document["overtime_minutes"] = 0
        document["half_day"] = False
        return document

    punch_in = document["punch_in"]
    punch_out = document["punch_out"]
    day = parse_date(document["date"])

    if punch_in is None:
        fail(422, "Presence status requires punch_in")

    document["late_minutes"] = compute_late_minutes(
        punch_in,
        employee,
        day,
    )

    if punch_out is None:
        document["work_hours"] = None
        document["overtime_minutes"] = 0
        document["half_day"] = False
        return document

    seconds = (punch_out - punch_in).total_seconds()

    if seconds <= 0 or seconds > 86400:
        fail(422, "punch_out must be after punch_in and within 24 hours")

    document["work_hours"] = compute_work_hours(punch_in, punch_out)
    document["overtime_minutes"] = compute_overtime(
        punch_out,
        employee,
        day,
    )
    document["half_day"] = document["work_hours"] < 4.50

    return document


def monthly_log_match(start: date, end: date, extra=None):
    query = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}

    if extra:
        query.update(extra)

    return query


def presence_fraction(document: dict) -> float:
    if document.get("status") not in PRESENT_STATUSES:
        return 0.0

    if document.get("half_day", False):
        return 0.5

    return 1.0


def bson_explain(collection_name: str, pipeline: list):
    command = {
        "explain": {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    }

    result = db.command(command)

    return json_util.loads(json_util.dumps(result))


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value):
        parse_shift_time(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value):
        parse_date(value)
        return value

    @field_validator("shift_end")
    @classmethod
    def different_shift_times(cls, value, info):
        start = info.data.get("shift_start", "09:30")

        if value == start:
            fail(422, "shift_start and shift_end must differ")

        return value


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_punched_at(cls, value):
        if value is not None:
            instant_from_ms(value)
        return value


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_punched_at(cls, value):
        if value is not None:
            instant_from_ms(value)
        return value


class RegularizeIn(StrictModel):
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def valid_times(cls, value):
        if value is not None:
            instant_from_ms(value)
        return value


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        fail(503, "MongoDB is unavailable")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["created_at"] = now_utc()

    try:
        employees.insert_one(document)
    except DuplicateKeyError:
        fail(409, "emp_code already exists")

    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_pagination(page, page_size)

    query = {}

    if department is not None:
        query["department"] = department

    total = employees.count_documents(query)

    cursor = (
        employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_employee(item) for item in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance punch-in
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employees.find_one({"emp_code": body.emp_code})

    if not employee:
        fail(404, "Employee not found")

    punched_at = (
        instant_from_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    day = attendance_date(punched_at, employee)

    document = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at,
            employee,
            day,
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        attendance.insert_one(document)
    except DuplicateKeyError:
        fail(409, "Already punched in for this attendance date")

    return serialize_attendance(document)


# ---------------------------------------------------------------------------
# Attendance punch-out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employees.find_one({"emp_code": body.emp_code})

    if not employee:
        fail(404, "Employee not found")

    punched_at = (
        instant_from_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    record = attendance.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        fail(404, "No punch-in found before the supplied time")

    if record.get("punch_out") is not None:
        fail(409, "This attendance record is already closed")

    seconds = (
        punched_at - record["punch_in"]
    ).total_seconds()

    if seconds <= 0 or seconds > 86400:
        fail(422, "punch_out must be after punch_in and within 24 hours")

    updated = attendance.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": compute_work_hours(
                    record["punch_in"],
                    punched_at,
                ),
                "overtime_minutes": compute_overtime(
                    punched_at,
                    employee,
                    parse_date(record["date"]),
                ),
                "half_day": compute_work_hours(
                    record["punch_in"],
                    punched_at,
                ) < 4.50,
            }
        },
    )

    if updated.modified_count != 1:
        fail(409, "Attendance record was already closed")

    record = attendance.find_one({"_id": record["_id"]})

    return serialize_attendance(record)


# ---------------------------------------------------------------------------
# Attendance list
# ---------------------------------------------------------------------------

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_pagination(page, page_size)

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from:
        parse_date(date_from)

    if date_to:
        parse_date(date_to)

    if date_from and date_to and date_from > date_to:
        fail(422, "date_from must not be after date_to")

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from

        if date_to:
            query["date"]["$lte"] = date_to

    if status is not None:
        if status not in ALL_STATUSES:
            fail(422, "Invalid attendance status")

        query["status"] = status

    total = attendance.count_documents(query)

    cursor = (
        attendance.find(query)
        .sort(
            [
                ("date", DESCENDING),
                ("emp_code", ASCENDING),
            ]
        )
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(item) for item in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Manual attendance correction
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    parse_date(date)

    employee = employees.find_one({"emp_code": emp_code})

    if not employee:
        fail(404, "Employee not found")

    record = attendance.find_one(
        {
            "emp_code": emp_code,
            "date": date,
        }
    )

    if not record:
        fail(404, "Attendance record not found")

    old = dict(record)

    old.setdefault("history", [])
    old.setdefault("half_day", False)
    old.setdefault("late_minutes", 0)
    old.setdefault("overtime_minutes", 0)
    old.setdefault("work_hours", None)
    old.setdefault("punch_in", None)
    old.setdefault("punch_out", None)

    proposed = dict(old)
    request = body.model_dump(exclude_unset=True)

    requested_status = request.get("status", old["status"])

    if requested_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in request and request["punch_in"] is not None:
            fail(422, "ABSENT and LEAVE cannot have punch times")

        if "punch_out" in request and request["punch_out"] is not None:
            fail(422, "ABSENT and LEAVE cannot have punch times")

        proposed["status"] = requested_status
        proposed["punch_in"] = None
        proposed["punch_out"] = None

    else:
        proposed["status"] = requested_status

        if "punch_in" in request:
            proposed["punch_in"] = instant_from_ms(
                request["punch_in"]
            )

        if "punch_out" in request:
            proposed["punch_out"] = instant_from_ms(
                request["punch_out"]
            )

        if proposed["punch_in"] is None:
            fail(422, "Presence status requires punch_in")

        if attendance_date(
            proposed["punch_in"],
            employee,
        ).isoformat() != date:
            fail(422, "punch_in must belong to the attendance date")

    proposed = compute_derived_fields(proposed, employee)

    changes = {}

    fields = [
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ]

    for field in fields:
        if old.get(field) != proposed.get(field):
            changes[field] = {
                "from": old.get(field),
                "to": proposed.get(field),
            }

    if not changes:
        fail(422, "The request does not change the attendance record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    proposed["history"] = old["history"] + [history_entry]

    update = {
        field: proposed.get(field)
        for field in fields
    }

    update["history"] = proposed["history"]

    # Compare-and-set prevents a concurrent correction from overwriting
    # another correction's history.
    result = attendance.update_one(
        {
            "_id": record["_id"],
            "history": old["history"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
        },
        {"$set": update},
    )

    if result.modified_count != 1:
        fail(409, "Attendance record changed concurrently; retry the request")

    updated_record = attendance.find_one({"_id": record["_id"]})

    return serialize_attendance(updated_record)


# ---------------------------------------------------------------------------
# Employee monthly analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    employee = employees.find_one({"emp_code": emp_code})
    if not employee:
        fail(404, "Employee not found")

    joined_on = parse_date(employee["joined_on"])
    working_start = max(start, joined_on)
    total_working_days = working_days(working_start, end)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$addFields": {"_parsed_date": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$gte": ["$_parsed_date", datetime.combine(working_start, time.min, tzinfo=UTC)]},
                    {"$in": [{"$dayOfWeek": "$_parsed_date"}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(attendance.aggregate(pipeline))
    summary = rows[0] if rows else {}
    present_days = summary.get("present_days", 0) or 0
    attendance_pct = round_half_up(present_days / total_working_days * 100, 2) if total_working_days else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": total_working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Department monthly summary
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    employee_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$emp_code", "$$employee_code"]},
                             "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$addFields": {"_parsed_date": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}}},
            ],
            "as": "log",
        }},
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "_employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$ne": [{"$ifNull": ["$log._parsed_date", None]}, None]}, {"$in": [{"$dayOfWeek": "$log._parsed_date"}, [2, 3, 4, 5, 6]]}, False]},
                ]},
                {"$cond": [{"$eq": ["$log.half_day", True]}, 0.5, 1]}, 0
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$log.work_hours", None]}, None]},
                ]}, "$log.work_hours", None
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$log.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$_employees"},
            "present_days": 1, "avg_work_hours": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]
    items = list(employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        item["avg_work_hours"] = round_half_up(item.get("avg_work_hours"), 2)
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = parse_month(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start.isoformat(),
                    "$lte": end.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {
                            "$rank": {},
                        }
                    },
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    return {
        "month": month,
        "items": list(attendance.aggregate(pipeline)),
    }


# ---------------------------------------------------------------------------
# Department daily trend
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date)
    end = parse_date(to_date)
    if end < start:
        fail(422, "'to' must not be earlier than 'from'")
    day_count = (end - start).days + 1
    if day_count > 92:
        fail(422, "The requested date range cannot exceed 92 days")
    if not employees.find_one({"department": department}):
        fail(404, "Department not found")

    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end, time.min, tzinfo=UTC)
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "seed": {"$literal": start_dt}, "days": {"$range": [0, day_count]}}},
        {"$unwind": "$days"},
        {"$addFields": {"trend_date": {"$dateAdd": {"startDate": "$seed", "unit": "day", "amount": "$days"}}}},
        {"$lookup": {
            "from": "employees",
            "let": {"current_date": "$trend_date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", {"$dateToString": {"date": "$$current_date", "format": "%Y-%m-%d", "timezone": "UTC"}}]},
                ]}}},
                {"$count": "count"},
            ],
            "as": "headcount_result",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day_string": {"$dateToString": {"date": "$trend_date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                     "dept": department},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day_string"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"$expr": {"$eq": ["$employee.department", "$$dept"]}}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.count", 0]}, 0]},
            "present_count": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [
                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1]}, 0
            ]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$trend_date"}, [2, 3, 4, 5, 6]]},
        }},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"trend_date": 1},
            "output": {"moving_avg_7d_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"trend_date": 1}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$trend_date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                       "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                       "attendance_rate": 1, "moving_avg_7d_raw": 1}},
    ]
    rows = list(employees.aggregate(pipeline))
    for row in rows:
        row["present_count"] = round_half_up(row.get("present_count", 0), 2)
        row["attendance_rate"] = round_half_up(row.get("attendance_rate"), 4)
        row["moving_avg_7d"] = round_half_up(row.pop("moving_avg_7d_raw", None), 4)
    return {"department": department, "items": rows}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_pagination(page, page_size)

    if endpoint == "attendance_list":
        query = {}

        if emp_code is not None:
            query["emp_code"] = emp_code

        if date_from:
            parse_date(date_from)

        if date_to:
            parse_date(date_to)

        if date_from and date_to and date_from > date_to:
            fail(422, "date_from must not be after date_to")

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = date_from

            if date_to:
                query["date"]["$lte"] = date_to

        if status is not None:
            if status not in ALL_STATUSES:
                fail(422, "Invalid attendance status")

            query["status"] = status

        pipeline = [
            {"$match": query},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ]

        explain = bson_explain("attendance_logs", pipeline)

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            fail(422, "emp_code and month are required")

        start, end = parse_month(month)

        employee = employees.find_one({"emp_code": emp_code})
        if not employee:
            fail(404, "Employee not found")
        joined_on = parse_date(employee["joined_on"])
        working_start = max(start, joined_on)

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
            {"$addFields": {"_parsed_date": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gte": ["$_parsed_date", datetime.combine(working_start, time.min, tzinfo=UTC)]},
                        {"$in": [{"$dayOfWeek": "$_parsed_date"}, [2, 3, 4, 5, 6]]},
                    ]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0
                ]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }},
        ]

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": bson_explain("attendance_logs", pipeline),
        }

    if endpoint == "department_summary":
        if not month:
            fail(422, "month is required")

        start, end = parse_month(month)

        employee_match = {"joined_on": {"$lte": end.isoformat()}}
        if department is not None:
            employee_match["department"] = department
        pipeline = [
            {"$match": employee_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$emp_code", "$$employee_code"]},
                                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                    {"$addFields": {"_parsed_date": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}}},
                ],
                "as": "log",
            }},
            {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$department",
                "_employees": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ne": [{"$ifNull": ["$log._parsed_date", None]}, None]}, {"$in": [{"$dayOfWeek": "$log._parsed_date"}, [2, 3, 4, 5, 6]]}, False]},
                    ]},
                    {"$cond": [{"$eq": ["$log.half_day", True]}, 0.5, 1]}, 0
                ]}},
                "avg_work_hours": {"$avg": {"$cond": [
                    {"$and": [
                        {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": [{"$ifNull": ["$log.work_hours", None]}, None]},
                    ]}, "$log.work_hours", None
                ]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$log.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "ON_DUTY"]}, 1, 0]}},
            }},
            {"$project": {
                "_id": 0, "department": "$_id", "headcount": {"$size": "$_employees"},
                "present_days": 1, "avg_work_hours": 1, "late_count": 1,
                "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            }},
            {"$sort": {"department": 1}},
        ]

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": bson_explain("employees", pipeline),
        }

    if endpoint == "late_leaderboard":
        if not month:
            fail(422, "month is required")

        start, end = parse_month(month)

        pipeline = [
            {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                        "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "late_count": {"$sum": 1}}},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$lookup": {"from": "employees", "localField": "_id",
                         "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                                  "output": {"rank": {"$rank": {}}}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id",
                          "name": "$employee.name", "department": "$employee.department",
                          "total_late_minutes": 1, "late_count": 1}},
        ])

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": bson_explain("attendance_logs", pipeline),
        }

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            fail(422, "department, from, and to are required")

        start = parse_date(from_date)
        end = parse_date(to_date)

        if end < start or (end - start).days + 1 > 92:
            fail(422, "Invalid trend date range")

        if not employees.find_one({"department": department}):
            fail(404, "Department not found")
        day_count = (end - start).days + 1
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "seed": {"$literal": start_dt}, "days": {"$range": [0, day_count]}}},
            {"$unwind": "$days"},
            {"$addFields": {"trend_date": {"$dateAdd": {"startDate": "$seed", "unit": "day", "amount": "$days"}}}},
            {"$lookup": {
                "from": "employees",
                "let": {"current_date": "$trend_date"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$department", department]},
                        {"$lte": ["$joined_on", {"$dateToString": {"date": "$$current_date", "format": "%Y-%m-%d", "timezone": "UTC"}}]},
                    ]}}},
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"day_string": {"$dateToString": {"date": "$trend_date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                        "dept": department},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day_string"]}}},
                    {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                    {"$unwind": "$employee"},
                    {"$match": {"$expr": {"$eq": ["$employee.department", "$$dept"]}}},
                    {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }},
            {"$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.count", 0]}, 0]},
                "present_count": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [
                    {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1]}, 0
                ]}}}},
                "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$trend_date"}, [2, 3, 4, 5, 6]]},
            }},
            {"$addFields": {"attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]}, None
            ]}}},
            {"$setWindowFields": {"sortBy": {"trend_date": 1},
                                  "output": {"moving_avg_7d_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$sort": {"trend_date": 1}},
            {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$trend_date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                          "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                          "attendance_rate": 1, "moving_avg_7d_raw": 1}},
        ]

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": bson_explain("employees", pipeline),
        }

    fail(422, "Unknown endpoint")