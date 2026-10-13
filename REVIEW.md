# REVIEW.md

## Review of the Starter Implementation

The starter implementation was reviewed for defects in attendance calculations, timestamp handling, input validation, concurrency, pagination, response formatting, and database query efficiency. The table below summarizes the identified defects, how they can be reproduced, and the corresponding corrections.

| # | Where (function / component) | What is wrong | How you'd notice it (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The calculation does not correctly handle overnight shifts in all cases. | Test attendance for a shift that starts before midnight and ends the following day. | Determine the correct shift-start instant and calculate lateness against it. |
| 2 | `compute_late_minutes` | The calculation does not consistently implement the required strict lateness threshold and whole-minute behavior. | Test punch-in times around the 10-minute threshold, including 09:40:00 and 09:40:01 for a 09:30 shift. | Apply the strict threshold and truncate elapsed minutes according to the contract. |
| 3 | `compute_work_hours` | Built-in `round()` uses ties-to-even rounding, which can differ from the required half-up rounding. | Test a duration whose calculated hours fall exactly halfway between two hundredths. | Use explicit decimal half-up rounding. |
| 4 | `compute_overtime` | Overnight shifts are not handled correctly when determining the shift-end instant. | Test a shift that starts before midnight and ends after midnight. | Construct the correct shift-end instant, including the following day when required. |
| 5 | `compute_overtime` | Overtime is counted without correctly applying the required 30-minute minimum. | Test punch-outs 29 minutes and 30 minutes after shift end. | Return zero below the threshold and count whole overtime minutes when the threshold is met. |
| 6 | `health` | The health endpoint does not verify database connectivity. | Stop MongoDB and call `GET /health`. | Ping MongoDB and return `503 Service Unavailable` when the database is unavailable. |
| 7 | `create_employee` | Checking for an existing employee before insertion does not prevent concurrent duplicate inserts. | Submit simultaneous requests with the same `emp_code`. | Add a unique index on `emp_code` and translate duplicate-key errors into `409 Conflict`. |
| 8 | `create_employee` | `datetime.now()` produces a naive datetime instead of explicitly representing UTC. | Inspect the stored creation timestamp and compare behavior across host timezone settings. | Store an explicit UTC datetime and serialize timestamps according to the API contract. |
| 9 | `EmployeeIn` | The input model does not adequately validate the required employee fields and formats. | Submit malformed employee codes, dates, email addresses, or shift times. | Add field validation for the formats and constraints required by the contract. |
| 10 | `PunchInIn` | The input model does not fully enforce the allowed attendance statuses and epoch-millisecond timestamp rules. | Submit an invalid status, a seconds-based timestamp, a float, or an out-of-range timestamp. | Validate status and timestamp values and return `422 Unprocessable Entity` for invalid inputs. |
| 11 | `punch_in` | The employee lookup result is not checked before employee fields are accessed. | Punch in using an unknown employee code. | Return `404 Not Found` when the employee does not exist. |
| 12 | `punch_in` | Checking `if body.punched_at` treats zero as if the timestamp were omitted. | Submit `punched_at=0` and verify that the supplied timestamp is rejected. | Distinguish a missing value from a supplied value and validate the latter explicitly. |
| 13 | `punch_in` | Local-time conversion can make the attendance date depend on the server's timezone. | Test the same timestamp on hosts configured with different timezones. | Convert epoch milliseconds to UTC and derive the attendance date using IST. |
| 14 | `punch_in` | A duplicate check followed by an insert is not atomic, so concurrent requests can create duplicate daily attendance records. | Send simultaneous punch-in requests for the same employee and attendance date. | Enforce a unique compound index on `(emp_code, date)` and return `409 Conflict` for duplicate records. |
| 15 | `punch_in` | The response includes an additional `id` field derived from MongoDB's internal `_id`, which is not part of the required response contract. | Compare the response fields with the documented API schema. | Return the documented fields without exposing MongoDB's internal identifier. |
| 16 | `list_employees` | The pagination offset uses `page * page_size`, which skips the first page's records. | With a page size of two, request page one and then page two. | Calculate the offset using `(page - 1) * page_size`. |
| 17 | `list_employees` | The total count includes employees that do not match the requested filter. | Filter by a department containing fewer employees than the company total. | Count documents using the same filter as the listing query. |
| 18 | `list_employees` | Pagination parameters are not adequately constrained. | Submit page zero, a negative page, or a page size above the allowed maximum. | Validate the page number and page size through FastAPI parameter validation. |
| 19 | `list_employees` | The query does not explicitly guarantee the required employee-code ordering. | Request multiple pages repeatedly and inspect their ordering. | Sort results by `emp_code` in ascending order. |
| 20 | `list_attendance` | The endpoint fetches all matching attendance records and performs sorting and pagination in Python, which is inefficient for large collections. | Test with a large attendance dataset and observe memory use and response latency. | Apply filtering, sorting, `skip`, and `limit` in MongoDB and create appropriate indexes. |
| 21 | `list_attendance` | The total is calculated from fetched documents rather than counting all documents that match the filters. | Compare totals when pagination is enabled and the collection contains many records. | Use a separate filtered count query before pagination. |
| 22 | `list_attendance` | Sorting only by date does not define a deterministic order for records with equal dates. | Create records for multiple employees on the same date and inspect the ordering. | Sort by date descending and `emp_code` ascending. |
| 23 | `list_attendance` | The response retains MongoDB's `_id` and adds an extra `id` field. | Inspect the response JSON for undocumented fields. | Exclude the internal identifier and return only the documented response fields. |
| 24 | `list_attendance` | Date and pagination inputs are not fully validated, including reversed date ranges. | Submit malformed dates, a `date_from` later than `date_to`, or invalid pagination values. | Validate the inputs and return `422 Unprocessable Entity` for invalid requests. |
| 25 | Application startup and database initialization | The required indexes are not created automatically when the application starts. | Inspect indexes on a fresh database and examine query plans. | Create the required indexes idempotently during startup. |
| 26 | Attendance timestamp and date handling | Timestamps and attendance calendar dates are not consistently normalized to UTC instants and IST dates. | Test timestamps around UTC and IST midnight boundaries. | Store timestamps as UTC datetimes and derive attendance dates using the contract's IST rules. |

## Reviewed and Not Considered Defects

- The default shift times (`09:30` and `18:30`) are acceptable if these defaults are defined by the contract.
- Using FastAPI for the HTTP layer and PyMongo for MongoDB access is appropriate.
- A unique employee-code index is necessary even when the application performs a duplicate pre-check; the pre-check alone is insufficient under concurrency.
- Loading local environment variables for development is acceptable, provided injected environment variables take precedence and credentials are not committed.
- Initializing a new attendance record with an empty `history` list is reasonable.
- Missing punch-out, regularization, analytics, and explain functionality should be recorded as implementation work when those endpoints are absent from the starter, rather than described as defects in existing implementations.