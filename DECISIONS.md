# Design Decisions

### 1. Indexes
I created six indexes. `uq_employee_code` prevents duplicate employee codes, while `department_joined` supports department and joining-date queries. `uq_attendance_employee_date` prevents duplicate daily attendance records. `attendance_date_employee` supports date filtering and sorting, `attendance_punch_lookup` helps find the latest eligible punch-in, and `attendance_date_status` supports date/status filtering. Indexes consume storage and slow writes, so I selected indexes based on the application's query patterns.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may initially find no existing record. The unique compound index allows only one insert to succeed. The other insert raises a duplicate-key error, which the API converts into HTTP `409 Conflict`. The database therefore enforces uniqueness rather than relying on an initial application-level check.

### 3. Ties
The leaderboard uses competition ranking, so equal late-minute totals receive the same rank. If two employees tie at the cutoff, both are returned, even if the result contains more entries than the requested limit. Employee code provides consistent ordering for tied rows.

### 4. Headcount
The department summary starts from employee records rather than attendance records. It can therefore count employees who have joined by the relevant date even when they have no attendance logs. Future joiners are excluded.

### 5. Scaling
For 100 times more data, I would run realistic load tests, inspect execution plans, review index selectivity, and monitor query latency. I would also evaluate MongoDB deployment capacity and connection-pool settings. If specific queries became bottlenecks, I would optimize their pipelines and indexes based on measured performance.