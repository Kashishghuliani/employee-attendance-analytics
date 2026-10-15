# REVIEW.md

## Defects Found in the Starter Code

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes()` | Calculates shift start on the punch-in date, producing incorrect results for overnight shifts. | Test a `22:00–06:00` shift with punch-in at `01:00` IST. | Determine the correct shift start date, accounting for overnight shifts. |
| 2 | Timestamp handling in helpers and endpoints | Timestamps are not consistently truncated to whole seconds before calculations and storage. | Submit a punch timestamp with milliseconds, such as `09:40:00.900`. | Truncate instants to whole seconds before calculations and storage. |
| 3 | `compute_work_hours()` | Uses Python's `round()`, which does not guarantee the required half-up rounding. | Test a duration whose hours value falls on a half-way rounding boundary. | Use decimal arithmetic with `ROUND_HALF_UP` to round to two decimal places. |
| 4 | `compute_overtime()` | Always places shift end on the supplied date, which is incorrect for overnight shifts. | Test a `22:00–06:00` shift with punch-out the following morning. | Calculate shift end on the correct date, including the following day for overnight shifts. |
| 5 | `compute_overtime()` | Counts overtime immediately after shift end instead of applying the required 30-minute minimum. | Test punch-out 20 minutes and 30 minutes after shift end. | Return overtime only when the elapsed duration reaches at least 30 minutes; otherwise return `0`. |
| 6 | `health()` | Returns success without checking whether MongoDB is reachable. | Stop MongoDB and call `/health`; the starter still returns `200`. | Ping MongoDB and return `503` if it is unavailable. |
| 7 | `create_employee()` | Checks for duplicate employee codes before insertion, which is unsafe under concurrent requests. | Send simultaneous employee-creation requests with the same `emp_code`. | Enforce uniqueness with a MongoDB unique index and convert duplicate-key errors to `409`. |
| 8 | `create_employee()` | Uses `datetime.now()` without explicitly setting UTC and returns `created_at` as a datetime instead of the required epoch-millisecond integer. | Create an employee and inspect the `created_at` field in the response. | Store the instant as a UTC BSON datetime and serialize it as epoch milliseconds in the response. |
| 9 | `list_employees()` | Calculates the pagination offset as `page * page_size`, skipping the first page. | Request `/employees?page=1&page_size=20`; the first 20 employees are skipped. | Use `(page - 1) * page_size` for the offset. |
| 10 | `list_employees()` | Counts all employees instead of counting only employees matching the department filter. | Request `/employees?department=CSE` when other departments exist; `total` includes employees from other departments. | Use `count_documents(q)` with the same filter as the list query. |
| 11 | `list_employees()` | Does not sort employees by `emp_code` ascending as required by the contract. | Insert employee codes in a different order and inspect the list response. | Sort by `emp_code` ascending in MongoDB before applying pagination. |
| 12 | `EmployeeIn` / `create_employee()` | Does not enforce all employee input constraints, including employee-code format, email format, valid date/time formats, string lengths, and different shift start/end times. | Submit malformed values or identical shift start and end times. | Add Pydantic validation for the contract's field constraints and shift-time rule. |
| 13 | `punch_in()` | Does not check whether the employee exists before accessing `emp["shift_start"]`. | Punch in using an unknown employee code; the starter can raise an unhandled error instead of returning `404`. | Check the employee lookup and return `404` when the employee does not exist. |
| 14 | `punch_in()` | Uses server-local time when converting timestamps and deriving the attendance date, instead of consistently applying UTC-instant and IST-calendar-date rules. | Submit an epoch timestamp near midnight UTC and compare the expected IST attendance date with the stored date. | Interpret epoch milliseconds as UTC, convert to IST for attendance-date and shift calculations, and store instants as UTC BSON datetimes. |
| 15 | `PunchInIn` / `punch_in()` | Accepts arbitrary status strings, although punch-in only allows `PRESENT`, `WFH`, and `ON_DUTY`. | Submit `status="LEAVE"` or another unsupported value. | Validate against the allowed presence statuses and return `422` for invalid input. |
| 16 | `punch_in()` | Uses a check-then-insert pattern that permits duplicate attendance records under concurrency. | Send simultaneous punch-in requests for the same employee and attendance date. | Enforce a unique compound index on `(emp_code, date)` and return `409` when a duplicate-key error occurs. |
| 17 | `punch_in()` / `list_attendance()` | Adds an `id` field derived from MongoDB's internal `_id`, which is not part of the expected API response. | Inspect either endpoint's response for an unexpected `id` field. | Return only the fields defined by the API contract. |
| 18 | `list_attendance()` | Loads every matching attendance document into Python before sorting and pagination, creating unnecessary memory usage for large datasets. | Run the endpoint against a large dataset and observe memory use and response time. | Perform filtering, sorting, `skip()` and `limit()` in MongoDB and add appropriate indexes. |
| 19 | `list_attendance()` | Sorts only by `date` descending, omitting the required secondary sort by `emp_code` ascending. | Create records for different employees on the same date and inspect their order. | Sort by `date` descending, then `emp_code` ascending. |
| 20 | `list_employees()` / `list_attendance()` | Does not enforce pagination limits or explicitly reject an invalid attendance date range where `date_from > date_to`. | Try `page=0`, `page_size=101`, or `date_from=2026-10-31&date_to=2026-10-01`. | Validate `page >= 1`, `1 <= page_size <= 100`, date formats, and `date_from <= date_to`; return `422` for invalid parameters. |
| 21 | `list_attendance()` | Does not validate `status` against the allowed attendance statuses. | Request `/attendance?status=INVALID`. | Restrict the parameter to supported status values and return `422` for invalid input. |

## Behaviors Examined and Found Not to Be Defects

- **Attendance filters:** `list_attendance()` applies the supplied employee, date, and status filters to its query. Its `total = len(docs)` reflects those filters, so the count itself is not incorrect. Loading every matching document into memory is a separate performance defect.
- **Attendance pagination formula:** The slice in `list_attendance()` uses the correct offset formula, `(page - 1) * page_size`. The employee-list endpoint does not.
- **Initial attendance values:** `punch_in()` initializes `punch_out` and `work_hours` to `null`, `overtime_minutes` to `0`, `half_day` to `false`, and `history` to `[]`. These initial values match the contract.
- **Punch-in duplicate handling:** The starter's duplicate check handles ordinary sequential requests. The defect is that the check alone is not safe under concurrent requests; a unique database index is needed for reliable enforcement.

## Missing Required Functionality in the Starter

The starter did not implement the following required endpoints:

- `POST /attendance/punch-out`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

These were missing assignment features rather than defects inside existing functions. I implemented these endpoints according to the API contract.