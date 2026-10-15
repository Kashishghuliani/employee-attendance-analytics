"""HROne Employee Attendance & Analytics API.

Run from the repository root with: uvicorn app.main:app --port 8000
All timestamps in HTTP requests/responses are epoch milliseconds; MongoDB stores UTC datetimes.
"""
from __future__ import annotations

import os
import json
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson.json_util import dumps as bson_dumps
from bson.decimal128 import Decimal128

load_dotenv()  
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}
MAX_EPOCH_MS = 4102444800000  


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    """Create indexes to enforce uniqueness and speed up common API queries."""

    # Employees: enforce unique employee codes and support department and join-date filtering.
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_joined",
    )
    # Supports queries that filter employees by their joining date.
    employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employee_joined",
    )

    # Attendance: prevent duplicate records for the same employee and date.
    attendance.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    # Supports attendance listing sorted by newest date, then employee code.
    attendance.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    # Helps find an employee's most recent punch-in for punch-out requests.
    attendance.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punchin",
    )
    # Supports attendance queries filtered by date and status.
    attendance.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_status_employee",
    )


@app.on_event("startup")
def startup() -> None:
    """Create required MongoDB indexes when the application starts."""
    create_indexes()


def bad_request(message: str) -> None:
    """Raise HTTP 422 when the request contains invalid data or parameters."""
    raise HTTPException(status_code=422, detail=message)


def parse_calendar_date(value: str) -> date:
    """Validate a YYYY-MM-DD date string and convert it to a Python date object."""
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        bad_request("Date must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    """Validate a YYYY-MM string and return the first and last dates of that month."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        bad_request("Month must be YYYY-MM")
    try:
        year, month = map(int, value.split("-"))
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        return first, last
    except ValueError:
        bad_request("Month must be YYYY-MM")


def epoch_ms_to_dt(value: Any, field_name: str = "timestamp") -> datetime:
    """Validate an epoch-millisecond timestamp and convert it to a UTC datetime."""
    if isinstance(value, bool) or not isinstance(value, int):
        bad_request(f"{field_name} must be an integer epoch-millisecond timestamp")
    if value < 100000000000 or value > MAX_EPOCH_MS:
        bad_request(f"{field_name} is outside the accepted epoch-millisecond range")
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    except (OverflowError, ValueError):
        bad_request(f"Invalid {field_name}")


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to epoch milliseconds in UTC, returning None if no value exists."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = value - epoch
    return (delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000)


def to_ist(value: datetime) -> datetime:
    """Convert a datetime to Indian Standard Time (IST), assuming UTC if timezone is missing."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def shift_datetime(day: date, hhmm: str) -> datetime:
    """Combine a calendar date and shift time into an IST-aware datetime."""
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def attendance_date_for(punch_in: datetime, emp: dict[str, Any]) -> date:
    """Determine the attendance date in IST, accounting for overnight shifts."""
    local = to_ist(punch_in)
    shift_start = time.fromisoformat(emp["shift_start"])
    shift_end = time.fromisoformat(emp["shift_end"])

    # For overnight shifts, punches before shift_end belong to the previous date.
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < shift_end:
        return local.date() - timedelta(days=1)

    return local.date()


def shift_bounds(record_date: str | date, emp: dict[str, Any]) -> tuple[datetime, datetime]:
    """Calculate a shift's start and end in UTC, accounting for overnight shifts."""
    d = parse_calendar_date(record_date) if isinstance(record_date, str) else record_date
    start = shift_datetime(d, emp["shift_start"])
    end_day = d + timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else d
    end = shift_datetime(end_day, emp["shift_end"])
    return start.astimezone(UTC), end.astimezone(UTC)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    """Round a number to the specified decimal places using half-up rounding."""
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def late_minutes_for(punch_in: datetime, emp: dict[str, Any], record_date: str) -> int:
    """Calculate late minutes after the 10-minute grace period, rounded down."""
    start, _ = shift_bounds(record_date, emp)
    seconds = int((punch_in - start).total_seconds())
    if seconds > 600:
        return seconds // 60
    return 0


def derived_fields(punch_in: Optional[datetime],punch_out: Optional[datetime],emp: dict[str, Any],record_date: str,) -> dict[str, Any]:
    """Calculate late minutes, work hours, overtime, and half-day status."""
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    punch_in = punch_in.replace(microsecond=0)
    if punch_out is not None:
        punch_out = punch_out.replace(microsecond=0)

    late = late_minutes_for(punch_in, emp, record_date)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    seconds = int((punch_out - punch_in).total_seconds())
    work_hours = round_half_up(Decimal(seconds) / Decimal(3600), 2)
    _, shift_end = shift_bounds(record_date, emp)
    overtime_seconds = int((punch_out - shift_end).total_seconds())
    overtime = overtime_seconds // 60 if overtime_seconds >= 1800 else 0

    return {"late_minutes": late,"work_hours": work_hours,"overtime_minutes": overtime,"half_day": work_hours < 4.50,}


def public_employee(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the employee's public fields and convert created_at to epoch milliseconds."""
    return {k: doc[k] for k in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at") if k in doc} | {"created_at": dt_to_ms(doc.get("created_at"))}


def public_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    """Format an attendance record for the API, converting timestamps to epoch milliseconds."""
    result = {k: doc.get(k) for k in ("emp_code", "date", "status", "work_hours", "late_minutes", "overtime_minutes")}
    result["half_day"] = doc.get("half_day") if doc.get("half_day") is not None else False
    result.update({"punch_in": dt_to_ms(doc.get("punch_in")), "punch_out": dt_to_ms(doc.get("punch_out"))})
    result["history"] = []

    for entry in doc.get("history", []) or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            changes[field] = {
                side: (dt_to_ms(val) if field in ("punch_in", "punch_out") else val)
                for side, val in change.items()
            }

        result["history"].append({
            "at": dt_to_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })

    return result


def month_pipeline_match(emp_code: str, first: date, last: date) -> dict[str, Any]:
    """Build a MongoDB filter for an employee's attendance within a date range."""
    return {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}


def build_employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list[dict[str, Any]]:
    """Build a MongoDB aggregation pipeline for an employee's monthly attendance statistics."""
    weekday_expr = {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}
    return [
        {"$match": month_pipeline_match(emp_code, first, last)},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, weekday_expr]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, "$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]


def working_days(start: date, end: date) -> int:
    """Count weekdays from start to end, inclusive, for an employee's monthly summary."""
    if end < start:
        return 0

    weeks, extra = divmod((end - start).days + 1, 7)
    count = weeks * 5
    for i in range(extra):
        if (start.weekday() + i) % 7 < 5:
            count += 1
    return count


def build_department_summary_pipeline(first: date,last: date,department: Optional[str] = None,) -> list[dict[str, Any]]:
    """Build a MongoDB aggregation pipeline for department-level attendance statistics."""
    # Starts from employees so headcount includes people with no logs.
    emp_match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "eligibleStart": {"$cond": [{"$gt": ["$joined_on", first.isoformat()]}, "$joined_on", first.isoformat()]}}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", "$$eligibleStart"]}, {"$lte": ["$date", last.isoformat()]}]}}},
            {"$set": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"}, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$eq": ["$logs._weekday", True]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}}, "work_hours_values": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, "$logs.late_minutes", 0]}}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$set": {"headcount": {"$size": "$headcount"}, "_work_hours": {"$filter": {"input": "$work_hours_values", "as": "h", "cond": {"$ne": ["$$h", None]}}}}},
        {"$set": {"avg_work_hours": {"$cond": [{"$gt": [{"$size": "$_work_hours"}, 0]}, {"$avg": "$_work_hours"}, None]}}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}
    ]


def build_late_leaderboard_pipeline(first: date,last: date,limit: int = 10,department: Optional[str] = None,) -> list[dict[str, Any]]:
    """Build a MongoDB pipeline ranking employees by total late minutes during a date range."""
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, "$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline += [
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}},
    ]

    return pipeline


def build_trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    """Build a MongoDB pipeline for daily department attendance trends and seven-day moving averages."""
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    scale, half = Decimal128("10000"), Decimal128("0.5")

    def round_half_up_expression(field: str) -> dict[str, Any]:
        """Create a MongoDB expression that rounds a value to four decimal places using half-up rounding."""
        return {"$cond": [{"$ne": [field, None]}, {"$toDouble": {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$toDecimal": field}, scale]}, half]}}, scale]}}, None]}

    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None}},
        {"$set": {"start": {"$literal": start_dt}}},
        {"$project": {"days": {"$map": {"input": {"$range": [0, (end - start).days + 1]}, "as": "offset", "in": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$days"},
        {"$set": {"date": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d", "timezone": "UTC"}}, "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$days"}, 2]}, {"$lte": [{"$dayOfWeek": "$days"}, 6]}]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [{"$match": {"$expr": {"$eq": ["$date", "$$day"]}}}, {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}, {"$match": {"employee.department": department}}, {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "metrics"}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$metrics.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$metrics.late_count", 0]}, 0]}, "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}, None]}}},
        {"$set": {"attendance_rate": round_half_up_expression("$attendance_rate")}},
        {"$setWindowFields": {"sortBy": {"days": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": round_half_up_expression("$moving_avg_7d")}},
        {"$sort": {"days": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}
    ]


def explain_query(endpoint: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return MongoDB execution statistics for the query used by a supported analytics endpoint."""
    if endpoint == "attendance_list":
        match: dict[str, Any] = {}
        for key in ("emp_code", "status"):
            if params.get(key) is not None:
                match[key] = params[key]

        if params.get("date_from") or params.get("date_to"):
            match["date"] = {}
            if params.get("date_from"):
                match["date"]["$gte"] = params["date_from"]
            if params.get("date_to"):
                match["date"]["$lte"] = params["date_to"]

        command = {
            "find": "attendance_logs",
            "filter": match,
            "sort": {"date": -1, "emp_code": 1},
            "hint": "ix_attendance_date_employee",
            "skip": (params["page"] - 1) * params["page_size"],
            "limit": params["page_size"],
        }
        return "attendance_logs", attendance.database.command(
            "explain", command, verbosity="executionStats"
        )

    if endpoint == "employee_monthly":
        first, last = parse_month(params["month"])
        emp = employees.find_one(
            {"emp_code": params["emp_code"]},
            {"_id": 0, "joined_on": 1},
        )
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found")

        eligible_start = max(first, parse_calendar_date(emp["joined_on"]))
        pipe = build_employee_monthly_pipeline(
            params["emp_code"], eligible_start, last
        )
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
            "allowDiskUse": True,
        }
        return "attendance_logs", attendance.database.command(
            "explain", command, verbosity="executionStats"
        )

    if endpoint == "department_summary":
        first, last = parse_month(params["month"])
        pipe = build_department_summary_pipeline(
            first, last, params.get("department")
        )
        command = {
            "aggregate": "employees",
            "pipeline": pipe,
            "cursor": {},
            "allowDiskUse": True,
        }
        return "employees", employees.database.command(
            "explain", command, verbosity="executionStats"
        )

    if endpoint == "late_leaderboard":
        first, last = parse_month(params["month"])
        pipe = build_late_leaderboard_pipeline(
            first, last, params["limit"], params.get("department")
        )
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
            "allowDiskUse": True,
        }
        return "attendance_logs", attendance.database.command(
            "explain", command, verbosity="executionStats"
        )

    if endpoint == "department_trend":
        start = parse_calendar_date(params["from"])
        end = parse_calendar_date(params["to"])
        pipeline = build_trend_pipeline(params["department"], start, end)
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
            "allowDiskUse": True,
        }
        explanation = employees.database.command(
            "explain", command, verbosity="executionStats"
        )
        return "employees", explanation

    bad_request("Unknown endpoint")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    """Validate employee creation data and ensure the shift times are different."""
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        json_schema_extra={"example": "user@example.com"},
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(json_schema_extra={"format": "date"})

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        """Validate that joined_on is a valid YYYY-MM-DD calendar date."""
        parse_calendar_date(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        """Reject employee data when shift_start and shift_end are identical."""
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchRequestBase(BaseModel):
    """Validate optional punch timestamps while rejecting explicitly supplied null values."""
    punched_at: StrictInt = Field(default=None, validate_default=False)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, data):
        """Reject requests that explicitly set punched_at to null."""
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return data

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        """Validate punched_at as an epoch-millisecond timestamp when provided."""
        if value is not None:
            epoch_ms_to_dt(value, "punched_at")
        return value


class PunchInRequest(PunchRequestBase):
    """Validate punch-in requests and restrict status to supported presence statuses."""
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(PunchRequestBase):
    """Validate punch-out requests for a specified employee."""
    model_config = ConfigDict(extra="ignore")

    emp_code: str


class RegularizeRequest(BaseModel):
    """Validate attendance regularization details and optional punch timestamps."""
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] = Field(default=None, validate_default=False)
    punch_in: StrictInt = Field(default=None, validate_default=False)
    punch_out: StrictInt = Field(default=None, validate_default=False)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_times(cls, value, info):
        """Validate provided punch timestamps as epoch milliseconds."""
        if value is not None:
            epoch_ms_to_dt(value, info.field_name)
        return value


# --------------------------------------------------------------------------- #
# Endpoints 
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Check MongoDB connectivity and return the service health status."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee, set the server-side creation timestamp, and handle duplicate codes."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Employee code already exists")

    return public_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees with an optional department filter and pagination."""
    filt = {"department": department} if department is not None else {}
    total = employees.count_documents(filt)
    docs = employees.find(filt, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)

    return {
        "items": [public_employee(d) for d in docs],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record an employee's punch-in and calculate initial attendance fields."""
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched = epoch_ms_to_dt(body.punched_at, "punched_at") if body.punched_at is not None else datetime.now(UTC)
    punched = punched.replace(microsecond=0)
    rec_date = attendance_date_for(punched, emp).isoformat()
    fields = derived_fields(punched, None, emp, rec_date)

    doc = {
        "emp_code": body.emp_code,
        "date": rec_date,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **fields,
        "history": [],
    }

    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Attendance record already exists for this date")

    return public_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Record an employee's punch-out and update calculated attendance fields."""
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched = epoch_ms_to_dt(body.punched_at, "punched_at") if body.punched_at is not None else datetime.now(UTC)
    punched = punched.replace(microsecond=0)

    record = attendance.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record already punched out")
    if punched <= record["punch_in"] or punched > record["punch_in"] + timedelta(hours=24):
        bad_request("punched_at must be after punch_in and no more than 24 hours later")

    new_derived = derived_fields(record["punch_in"], punched, emp, record["date"])
    updated = attendance.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {"$set": {"punch_out": punched, **new_derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Attendance record was updated concurrently")

    return public_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    date_to: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records with optional filters, date validation, sorting, and pagination."""
    filt: dict[str, Any] = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code
    if status is not None:
        filt["status"] = status

    parsed_from = parse_calendar_date(date_from) if date_from is not None else None
    parsed_to = parse_calendar_date(date_to) if date_to is not None else None

    if parsed_from is not None and parsed_to is not None and parsed_from > parsed_to:
        bad_request("date_from must be on or before date_to")

    if parsed_from is not None or parsed_to is not None:
        filt["date"] = {}
        if parsed_from is not None:
            filt["date"]["$gte"] = parsed_from.isoformat()
        if parsed_to is not None:
            filt["date"]["$lte"] = parsed_to.isoformat()

    total = attendance.count_documents(filt)
    docs = (
        attendance.find(filt, {"_id": 0})
        .hint("ix_attendance_date_employee")
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [public_attendance(d) for d in docs],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., json_schema_extra={"format": "date"}),
    body: RegularizeRequest = ...,
):
    """Correct an attendance record, recalculate derived fields, and append change history."""
    parse_calendar_date(date)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    old = attendance.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    requested = body.model_fields_set
    if "status" in requested and body.status is None:
        bad_request("status cannot be null")
    if "punch_in" in requested and body.punch_in is None:
        bad_request("punch_in cannot be null")
    if "punch_out" in requested and body.punch_out is None:
        bad_request("punch_out cannot be null")

    new_status = body.status if "status" in requested else old["status"]
    if new_status in ("ABSENT", "LEAVE") and ("punch_in" in requested or "punch_out" in requested):
        bad_request("ABSENT and LEAVE cannot have punch times")

    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None
    else:
        new_in = epoch_ms_to_dt(body.punch_in, "punch_in") if "punch_in" in requested else old.get("punch_in")
        new_out = epoch_ms_to_dt(body.punch_out, "punch_out") if "punch_out" in requested else old.get("punch_out")

        # R1: Truncate timestamps to whole seconds.
        if new_in is not None:
            new_in = new_in.replace(microsecond=0)
        if new_out is not None:
            new_out = new_out.replace(microsecond=0)

        if new_in is None:
            bad_request("Presence statuses require punch_in")
        if attendance_date_for(new_in, emp).isoformat() != date:
            bad_request("punch_in must belong to the attendance date")
        if new_out is not None and (new_out <= new_in or new_out > new_in + timedelta(hours=24)):
            bad_request("punch_out must be after punch_in and no more than 24 hours later")

    computed = derived_fields(new_in, new_out, emp, date)
    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **computed,
    }

    compare_fields = [
        "status", "punch_in", "punch_out", "late_minutes",
        "work_hours", "overtime_minutes", "half_day",
    ]
    changes = {}
    for field in compare_fields:
        before = old.get(field, False if field == "half_day" else None)
        after = proposed[field]
        if before != after:
            changes[field] = {"from": before, "to": after}

    if not changes:
        bad_request("Regularization does not change any fields")

    at = datetime.now(UTC).replace(microsecond=0)
    history_entry = {
        "at": at,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare-and-swap prevents concurrent edits from losing history.
    cas = {"emp_code": emp_code, "date": date, "_id": old["_id"]}
    for field in compare_fields:
        cas[field] = old.get(field, False if field == "half_day" else None)

    updated = attendance.find_one_and_update(
        cas,
        {"$set": proposed, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was updated concurrently",
        )

    return public_attendance(updated)



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Return an employee's monthly attendance summary, considering their joining date."""
    first, last = parse_month(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    eligible_start = max(first, parse_calendar_date(emp["joined_on"]))
    if eligible_start > last:
        data = {}
        wd = 0
    else:
        pipeline = build_employee_monthly_pipeline(emp_code, eligible_start, last)
        agg = list(attendance.aggregate(pipeline))
        data = agg[0] if agg else {}
        wd = working_days(eligible_start, last)

    present = float(data.get("present_days", 0))
    pct = round_half_up(Decimal(str(present)) / Decimal(wd) * 100, 4) if wd else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": data.get("leave_days", 0),
        "late_count": data.get("late_count", 0),
        "total_late_minutes": data.get("total_late_minutes", 0),
        "total_overtime_minutes": data.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Return monthly attendance statistics for all departments or one specified department."""
    first, last = parse_month(month)
    pipeline = build_department_summary_pipeline(first, last, department)
    rows = list(employees.aggregate(pipeline, allowDiskUse=True))

    for row in rows:
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
        if row.get("avg_work_hours") is not None:
            row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2)

    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Return the monthly late-arrival leaderboard, optionally filtered by department."""
    first, last = parse_month(month)
    pipeline = build_late_leaderboard_pipeline(first, last, limit, department)
    rows = list(attendance.aggregate(pipeline, allowDiskUse=True))

    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", json_schema_extra={"format": "date"}),
    to_date: str = Query(..., alias="to", json_schema_extra={"format": "date"}),
):
    """Return daily attendance trends for a department over a validated date range."""
    start = parse_calendar_date(from_date)
    end = parse_calendar_date(to_date)

    if end < start:
        bad_request("to must be on or after from")
    if (end - start).days + 1 > 92:
        bad_request("Date range must not exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_trend_pipeline(department, start, end)
    rows = list(employees.aggregate(pipeline, allowDiskUse=True))

    for row in rows:
        row["present_count"] = round_half_up(row.get("present_count", 0), 2)
        if row.get("attendance_rate") is not None:
            row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)

    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    date_to: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    from_date: Optional[str] = Query(None, alias="from", json_schema_extra={"format": "date"}),
    to_date: Optional[str] = Query(None, alias="to", json_schema_extra={"format": "date"}),
):
    """Validate explain-query parameters and return MongoDB execution statistics as JSON."""
    params = {
        "emp_code": emp_code,
        "date_from": date_from,
        "date_to": date_to,
        "status": status,
        "page": page,
        "page_size": page_size,
        "month": month,
        "department": department,
        "limit": limit,
        "from": from_date,
        "to": to_date,
    }

    if endpoint == "attendance_list":
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status")
        start = parse_calendar_date(date_from) if date_from else None
        end = parse_calendar_date(date_to) if date_to else None
        if start and end and start > end:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    elif endpoint in ("employee_monthly", "department_summary", "late_leaderboard"):
        if not month:
            raise HTTPException(status_code=422, detail="month is required for this endpoint")
        parse_month(month)
        if endpoint == "employee_monthly" and not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for this endpoint")

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for this endpoint")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from and to are required for this endpoint")

        start = parse_calendar_date(from_date)
        end = parse_calendar_date(to_date)
        if end < start:
            bad_request("to must be on or after from")
        if (end - start).days + 1 > 92:
            bad_request("Date range must not exceed 92 days")
        if not employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(status_code=404, detail="Department not found")

    try:
        collection, explanation = explain_query(endpoint, params)
        explanation_json = json.loads(bson_dumps(explanation))
        return {
            "endpoint": endpoint,
            "collection": collection,
            "explain": explanation_json,
        }
    except HTTPException:
        raise
    except PyMongoError as exc:
        print("EXPLAIN ERROR:", repr(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc