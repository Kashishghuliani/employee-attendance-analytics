# DECISIONS.md

## 1. Indexes

**1. `employees.emp_code` — Unique Index**

Every employee has an employee code. This index makes searches by employee code faster and prevents two employees from having the same code.

**2. `employees.department + joined_on + emp_code` — Compound Index**

This index supports employee queries that filter by department and joining date. Employee code is also included to help support consistent results. The field order is intentional because department and joining date are the main filters this index is designed to support.

**3. `employees.joined_on` — Index**

This index supports searches based on an employee’s joining date, especially when the query does not include a department filter.

**4. `attendance_logs.emp_code + date` — Unique Compound Index**

An employee should have only one attendance record for a particular date. This index enforces that rule directly in the database, even when two punch-in requests arrive at nearly the same time.

**5. `attendance_logs.date + emp_code` — Compound Index**

This index supports attendance queries organized by date, with employee code as the next field. It is useful for date-focused attendance views.

**6. `attendance_logs.emp_code + punch_in` — Compound Index**

This index supports queries that retrieve an employee’s attendance records using their employee code and punch-in time. It can help when finding or sorting recent attendance records for one employee.

**7. `attendance_logs.date + status + emp_code` — Compound Index**

This index supports queries that filter attendance records by date and status, with employee code as another matching field. It is useful for date-based summaries of statuses such as present or on leave.

I avoided adding a separate index on status alone to limit unnecessary index maintenance during writes. I would reconsider this decision if query-plan measurements showed that it would improve performance.

## 2. Punch-In Race

Two identical punch-in requests can arrive at almost the same time. Both might check whether an attendance record exists before either request inserts one. 

The unique compound index on `emp_code` and `date` ensures that only one record can be inserted for that employee on that date. The other request encounters a `DuplicateKeyError`, which the API converts into an HTTP 409 Conflict response. This protects the data even when requests run concurrently.

## 3. Ties 

The leaderboard uses MongoDB’s `$rank` operation to rank employees by their total late minutes. It returns every employee whose rank is within the requested limit.

If multiple employees have the same rank at the cutoff, all of them are included. Therefore, the response can contain more employees than the requested limit. Employee code is used to order the results consistently.

## 4. Headcount

The department summary starts with eligible employees and then looks up their attendance records. It preserves employees who have no matching attendance logs and counts distinct employee codes.

This approach keeps department headcount accurate because an employee is not excluded simply because they have not recorded attendance.

## 5. One thing you would change

If the application had 100 times more data, I would first inspect query plans and execution statistics to identify actual performance bottlenecks. I would add or change indexes only when measurements showed a clear benefit.

If repeated analytics aggregations became expensive, I would consider maintaining daily or monthly summary data. I would also ensure that attendance regularizations update those summaries correctly so that the analytics remain accurate.