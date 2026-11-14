# Requirements Document

## Introduction

Coroner is a crash forensics tool for containers. Milestone M0 (Crash Evidence Survival) builds the technical foundation: diagnostic telemetry from a Python application is preserved outside the application's own process, so the evidence survives a crash, a SIGKILL, or a Docker out-of-memory kill. M0 delivers a FastAPI application with a reproducible memory leak, an embedded telemetry probe, an independent collector running in a separate Docker container, persistent JSONL evidence storage, and a Docker Compose environment for three repeatable crash experiments. M0 does not include analysis, diagnosis, or reporting beyond reporting gaps and uncertainty in the evidence.

## Glossary

- **Coroner**: The complete M0 system, made up of the Demo_App, Probe, Collector, Evidence_Store, and Experiment_Runner.
- **Demo_App**: The FastAPI application that exposes a reproducible memory leak and runs in its own Docker container.
- **Probe**: The lightweight Python telemetry module embedded in the Demo_App that samples measurements and sends them to the Collector.
- **Collector**: The independent Python service that runs in a separate Docker container, receives samples from the Probe, and writes them to the Evidence_Store.
- **Evidence_Store**: The persistent JSONL files, held on a Docker volume, that contain all records written by the Collector.
- **Evidence_Writer**: The Python module that serializes records into JSONL lines for the Evidence_Store.
- **Evidence_Reader**: The Python module that parses and validates Evidence_Store files and produces per-run summaries.
- **Sample**: One telemetry record containing a UTC timestamp, process RSS, request count, event-loop delay, process identifier, container identifier, and a sequence number.
- **Capture_Time**: The UTC timestamp recorded by the Probe in a Sample at the moment the Sample is taken (the Probe timestamp).
- **Receive_Time**: The UTC time at which the Collector receives a Sample, stored in the Evidence_Store record in addition to the Capture_Time.
- **Health_Endpoint**: The Collector HTTP endpoint whose response body includes a boolean field storage_ok.
- **Storage_Ok**: The value of the storage_ok field in the Health_Endpoint body. The value is true only when the Collector has completed a successful writability and persistence probe of the Evidence_Store volume. An HTTP 200 status alone does not establish Storage_Ok.
- **Gap_Record**: An Evidence_Store record written by the Collector that states a sample interval or range was not received, including the start time, end time, and, where the sequence range is known, the first and last missing sequence numbers, the number of missing samples, and a reconciliation field stating whether the range overlaps another Gap_Record. A Probe-reported drop Gap_Record states explicitly whether its dropped sequence range is known or uncertain.
- **Missing_Sample_Count**: For a Run_Identifier, the number of distinct sequence numbers that are known to be missing, computed as the size of the union of the missing sequence number ranges of all sequence discontinuity Gap_Records only. Probe-reported drop Gap_Records and silence timeout Gap_Records do not contribute to this count.
- **Unreconciled_Drop_Count**: For a Run_Identifier, the number of Samples the Probe reported as dropped whose sequence range is unknown or uncertain, reported separately from the Missing_Sample_Count together with an uncertainty flag.
- **Malformed_Line**: A line in an Evidence_Store file that is not valid UTF-8 JSON, including an incomplete fragment isolated by a repair.
- **Durability_Level**: The persistence guarantee the Collector satisfies before acknowledging a record. Exactly one of Process_Durable or Crash_Durable.
- **Process_Durable**: The Durability_Level in which the Collector writes and flushes the record to the operating system, so that the record survives a crash of the Collector process or the Demo_App but not necessarily a host or operating system crash.
- **Crash_Durable**: The Durability_Level in which the Collector writes, flushes, and calls os.fsync on the file (and calls fsync on the containing directory when the file is created) before acknowledging, so that the record survives a host or operating system crash, subject to the storage caveat documented in Requirement 14.
- **Repair_Record**: An Evidence_Store record written by the Collector when it isolates an incomplete last line, containing the original fragment content (base64-encoded), its byte length, the file name, and the line number.
- **Crash_Endpoint**: The Demo_App HTTP endpoint that calls os._exit(1), which exits the process immediately without running exception handlers, shutdown handlers, atexit hooks, or finalizers.
- **Error_Endpoint**: The Demo_App HTTP endpoint that raises an ordinary Python exception, which the web framework converts to an HTTP 500 response without terminating the process.
- **Destructive_Endpoints**: The Demo_App endpoints that are intentionally harmful: the Crash_Endpoint, the Leak_Endpoint, and the Error_Endpoint.
- **Destructive_Endpoints_Flag**: The environment variable CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS that, when set to 1, enables the Destructive_Endpoints.
- **Sample_Interval**: The configured period between consecutive Probe samples, expressed in milliseconds.
- **RSS**: Resident set size of the Demo_App process, in bytes, as read from the operating system.
- **Event_Loop_Delay**: The measured lateness, in milliseconds, of a scheduled asyncio callback relative to its intended run time.
- **Request_Count**: The cumulative number of HTTP requests the Demo_App has received since process start.
- **Leak_Endpoint**: The Demo_App HTTP endpoint that retains a caller-specified number of bytes in process memory on each call.
- **Experiment_Runner**: The scripted tooling that executes Experiment A, B, or C against the Docker Compose environment and records the outcome.
- **Experiment_A**: The controlled fatal-process-exit test in which the Demo_App Crash_Endpoint calls os._exit(1), terminating the process immediately without running handlers. The expected container exit code is 1 and the expected OOMKilled flag is false. Experiment_A is not an exception test.
- **Experiment_B**: The experiment in which the Demo_App process is terminated with SIGKILL. A Sample captured before the SIGKILL send time may be received and persisted by the Collector after the SIGKILL send time because of an in-flight request.
- **Experiment_C**: The experiment in which the Demo_App container is terminated by a genuine Docker out-of-memory kill. Its mandatory pass conditions are the OOMKilled flag, the exit code, Collector survival, preserved pre-crash evidence, and measurable memory growth.
- **Diagnostic_Metrics**: Values computed by the Experiment_Runner for Experiment_C and recorded in the Run_Manifest without affecting the pass or fail outcome: rss_monotonic_tail, peak_rss_ratio_of_limit, and the Sample count.
- **Sampling_Limitations**: The statements the Experiment_Runner records in the Run_Manifest and summary about what the Samples cannot show, including the Sample_Interval relative to the leak speed, the unobserved final seconds before the out-of-memory kill, and the possibility that the peak RSS understates the memory limit.
- **Container_State**: The state reported by Docker for a container, including its status, OOMKilled flag, and exit code, as read from Docker inspection.
- **Run_Manifest**: A JSON record, written by the Experiment_Runner for each experiment run, that lists the experiment name, run identifier, start and end times, observed Container_State, Evidence_Store file path, configured Durability_Level, and (for Experiment_B) the SIGKILL send time and the Sample timing classification, and (for Experiment_C) the Docker inspection evidence, the Diagnostic_Metrics, and the Sampling_Limitations.
- **Experiment_Runner_Environment**: The host on which the Experiment_Runner runs, which is either a POSIX shell host or Windows PowerShell with Docker Desktop using Linux containers.
- **Run_Identifier**: A unique string that identifies one experiment run and is attached to every record produced during that run.

## Requirements

### Requirement 1: Demo Application with Reproducible Memory Leak

**User Story:** As a developer, I want a FastAPI application that leaks memory on demand, so that I can reproducibly drive a container toward a crash.

#### Acceptance Criteria

1. THE Demo_App SHALL run as a FastAPI application inside its own Docker container.
2. WHEN a client sends a request to the Leak_Endpoint with a byte count between 1 and 104,857,600 (100 MiB) inclusive, THE Demo_App SHALL retain exactly that number of additional bytes in process memory until the process terminates.
3. WHEN a client sends a valid request to the Leak_Endpoint, THE Demo_App SHALL return a success response within 5 seconds containing the cumulative total of bytes retained since process start, including the bytes from that request.
4. IF a client sends a Leak_Endpoint request with a byte count that is missing, not an integer, less than 1, or greater than 104,857,600, THEN THE Demo_App SHALL return an HTTP 422 response indicating the invalid byte count, retain no additional memory, and leave the cumulative total of retained bytes unchanged.
5. WHILE the Demo_App process is running, THE Demo_App SHALL return an HTTP 200 response within 1 second to each request to the health endpoint.
6. WHEN a client sends a request to the Crash_Endpoint, THE Demo_App SHALL call os._exit(1), terminating the process immediately with exit code 1 and causing the Docker container to stop.
7. WHEN the Demo_App retains bytes for a Leak_Endpoint request, THE Demo_App SHALL fill the retained bytes with non-zero content so that the resident set size (RSS) of the process increases by at least 90% of the requested byte count.
8. IF the process cannot allocate the requested bytes, THEN THE Demo_App SHALL either return an error response indicating allocation failure without changing the cumulative total of retained bytes, or be terminated by the operating system out-of-memory killer.
9. WHEN a client sends a request to the Error_Endpoint, THE Demo_App SHALL raise an ordinary Python exception that results in an HTTP 500 response, and THE Demo_App process SHALL continue running.
10. WHILE the Destructive_Endpoints_Flag is not set to 1, THE Demo_App SHALL not route the Crash_Endpoint, the Leak_Endpoint, or the Error_Endpoint, and SHALL return HTTP 404 for requests to them.
11. WHILE the Destructive_Endpoints_Flag is set to 1, THE Demo_App SHALL route the Crash_Endpoint, the Leak_Endpoint, and the Error_Endpoint.

### Requirement 2: Telemetry Probe Sampling

**User Story:** As a developer, I want a lightweight probe embedded in the application, so that real measurements are taken from inside the process before it dies.

#### Acceptance Criteria

1. WHILE the Demo_App is running, THE Probe SHALL produce one Sample every Sample_Interval, with each Sample produced within ±10 percent of the Sample_Interval measured between consecutive Sample timestamps.
2. THE Probe SHALL record in each Sample a UTC timestamp in ISO 8601 format with millisecond precision (for example, YYYY-MM-DDTHH:mm:ss.sssZ).
3. THE Probe SHALL record in each Sample the RSS of the Demo_App process, read from the operating system, as an integer number of bytes greater than or equal to 0.
4. THE Probe SHALL record in each Sample the Request_Count as an integer greater than or equal to 0, representing the cumulative number of requests received by the Demo_App since process start.
5. THE Probe SHALL record in each Sample the Event_Loop_Delay measured during that Sample_Interval, as a number of milliseconds greater than or equal to 0.
6. THE Probe SHALL record in each Sample the process identifier and the container identifier of the Demo_App.
7. THE Probe SHALL record in each Sample a sequence number that starts at 0 for the first Sample of a process and increases by 1 for each subsequent Sample.
8. WHERE the Sample_Interval is set through an environment variable to an integer between 100 and 60000 milliseconds, THE Probe SHALL use the configured value in place of the default of 500 milliseconds.
9. IF a measurement cannot be read, THEN THE Probe SHALL record the field as null together with a reason string of 1 to 200 characters, still produce the Sample with all other fields populated, and continue sampling at the next Sample_Interval.
10. THE Probe SHALL add no more than 5 percent to the Demo_App CPU usage while the Demo_App handles no requests, measured over 60 seconds.
11. IF the environment variable for the Sample_Interval is not an integer between 100 and 60000 milliseconds, THEN THE Probe SHALL use the default of 500 milliseconds and emit one warning indicating the configured value was rejected.

### Requirement 3: Probe Delivery Independent of Application Health

**User Story:** As a developer, I want the probe to push samples out immediately, so that evidence leaves the process before any crash and does not depend on shutdown handlers.

#### Acceptance Criteria

1. WHEN the Probe produces a Sample, THE Probe SHALL begin sending the Sample to the Collector within 100 milliseconds of Sample creation and before the next Sample_Interval tick.
2. THE Probe SHALL deliver evidence to the Collector without relying on any shutdown handler, signal handler, or atexit hook.
3. IF the Collector is unreachable or a delivery attempt fails, THEN THE Probe SHALL discard the unsent Sample, increment the dropped-sample counter by 1, and continue sampling at the configured Sample_Interval without retrying the discarded Sample.
4. WHEN the Probe completes a successful delivery after one or more failed deliveries, THE Probe SHALL include the dropped-sample counter value in the next Sample sent and then reset the counter to 0 only after the Collector acknowledges that Sample.
5. IF a delivery attempt does not complete within 200 milliseconds, THEN THE Probe SHALL abandon the attempt, treat the Sample as undelivered, and apply the same handling as criterion 3.
6. THE Probe SHALL perform delivery without blocking the Demo_App event loop for more than 10 milliseconds per Sample.
7. IF the dropped-sample counter reaches its maximum value of 4,294,967,295, THEN THE Probe SHALL hold the counter at that value and continue sampling.

### Requirement 4: Independent Collector

**User Story:** As a developer, I want the collector to run in a separate container, so that it keeps running and holding evidence when the application dies.

#### Acceptance Criteria

1. THE Collector SHALL run as a Python service in a Docker container that is separate from the Demo_App container.
2. WHEN the Collector receives a valid Sample that includes a Run_Identifier, THE Collector SHALL append the Sample to the Evidence_Store as one record together with the Receive_Time in UTC, formatted as ISO 8601 with millisecond precision or finer, and SHALL preserve the Sample Capture_Time unchanged in the same record.
3. WHEN the Collector appends a record, THE Collector SHALL persist the record to the configured Durability_Level before acknowledging receipt to the Probe, and SHALL acknowledge within 500 milliseconds of receipt under a load of 1 Sample per second.
4. WHILE the Demo_App container is stopped, killed, or restarting, THE Collector SHALL continue running, keep serving its health endpoint, and keep every record already stored in the Evidence_Store unchanged.
5. IF the Collector receives a malformed Sample (not parseable as JSON, missing a required Sample field, or a field of the wrong type), THEN THE Collector SHALL reject it with an HTTP 422 response, append a rejection record to the Evidence_Store containing the receive time in UTC and a reason identifying the failed validation, and not append the malformed Sample as a valid Sample.
6. THE Collector SHALL store each record as one JSON object per line in a JSONL file, with each line terminated by a single newline character and containing no embedded newline characters.
7. THE Collector SHALL tag each stored Sample with the Run_Identifier supplied by the Probe, using a non-empty string of 1 to 64 characters.
8. IF the Collector fails to write or flush a record to durable storage, THEN THE Collector SHALL NOT acknowledge receipt to the Probe, SHALL respond with an error indicating the storage failure, and SHALL keep running and serving its health endpoint.
9. IF the Collector receives a Sample with a missing, empty, or longer than 64 characters Run_Identifier, THEN THE Collector SHALL reject it with an HTTP 422 response and append a rejection record to the Evidence_Store containing the receive time in UTC and a reason indicating the invalid Run_Identifier.
10. THE Collector SHALL support exactly two Durability_Levels, Process_Durable and Crash_Durable, and SHALL select the level through an environment variable named CORONER_DURABILITY with the values "process" and "crash".
11. WHERE the environment variable CORONER_DURABILITY is unset or empty, THE Collector SHALL use Crash_Durable as the M0 default Durability_Level.
12. IF the environment variable CORONER_DURABILITY is set to a value other than "process" or "crash", THEN THE Collector SHALL fail to start and report an error identifying the invalid value.
13. WHILE the Durability_Level is Crash_Durable, THE Collector SHALL write, flush, and call os.fsync on the Evidence_Store file before acknowledging each record, and SHALL call fsync on the containing directory when it creates a new Evidence_Store file.
14. WHILE the Durability_Level is Process_Durable, THE Collector SHALL write and flush each record to the operating system before acknowledging the record.
15. THE Collector SHALL claim the Crash_Durable level in any acknowledgement, metadata, evidence record, or documentation only when the Collector performs the flush and fsync steps of criterion 13 before acknowledgement.
16. WHEN the Collector starts a run's Evidence_Store file, THE Collector SHALL record the configured Durability_Level in a run metadata record in that file, and THE Experiment_Runner SHALL record the same Durability_Level in the Run_Manifest.
17. WHEN the Collector receives a request on the Health_Endpoint, THE Collector SHALL respond with a body that includes the boolean field storage_ok, and SHALL set storage_ok to true only after the Collector has performed a successful writability and persistence probe of the Evidence_Store volume.
18. IF the Collector cannot complete the writability and persistence probe, THEN THE Collector SHALL set storage_ok to false in the Health_Endpoint body and keep serving the Health_Endpoint.

### Requirement 5: Persistent Evidence Storage

**User Story:** As a developer, I want evidence stored in persistent, human-readable files, so that I can inspect it after any crash and after container restarts.

#### Acceptance Criteria

1. THE Evidence_Store SHALL reside on a Docker volume that is mounted into the Collector container.
2. THE Evidence_Store SHALL keep exactly one JSONL file per Run_Identifier, and THE Collector SHALL append all records for that Run_Identifier to that file only.
3. WHEN the Collector container restarts, THE Evidence_Store SHALL retain every record that the Collector acknowledged before the restart, with no record lost, altered, or duplicated.
4. THE Evidence_Writer SHALL write each record as a single line of valid UTF-8 JSON, terminated by exactly one newline character, containing no embedded raw newline characters.
5. IF the last line of an Evidence_Store file does not end with a newline character or is not valid JSON when the Collector starts or before the Collector appends any further record to that file, THEN THE Collector SHALL isolate the incomplete line by appending a newline character so that new records start on a fresh line, and SHALL write a Repair_Record that preserves the original fragment bytes (base64-encoded) together with the fragment byte length, the file name, the line number, and the file's Run_Identifier. The isolated fragment remains in the file as a Malformed_Line for audit purposes.
6. IF the Collector repairs an incomplete last line by truncating the file instead of appending a newline character, THEN THE Collector SHALL first write the Repair_Record containing the full original fragment and persist it as stated in criterion 7, and SHALL truncate the file only after that persistence completes.
7. WHEN the Collector writes a Repair_Record, THE Collector SHALL persist the repair (the isolating newline and the Repair_Record) to the configured Durability_Level, using flush and fsync while the level is Crash_Durable, before accepting any new append to that file.
8. THE Collector SHALL not lose or alter the original bytes of an incomplete line, so that the fragment is recoverable either from the Evidence_Store file itself or from the Repair_Record.
9. WHEN the Evidence_Reader reads an Evidence_Store file, THE Evidence_Reader SHALL parse each complete line into a record whose field names and values are equal to those that were written (round-trip property).
10. IF the Evidence_Reader encounters a Malformed_Line, THEN THE Evidence_Reader SHALL report the file, the line number, and the byte length of that line, isolate the line from the valid records, SHALL NOT treat the line as a valid evidence record, SHALL NOT count the line in any Sample count, and SHALL continue parsing the remaining lines, including lines that were isolated by a repair and lines that have a corresponding Repair_Record.
11. THE Evidence_Reader SHALL be a Python module that parses and validates Evidence_Store files and is used by the automated tests and the Experiment_Runner.
12. THE Evidence_Writer SHALL be a Python module that serializes records to JSONL lines and is used by the Collector.

### Requirement 6: Gap and Uncertainty Reporting

**User Story:** As a developer, I want missing telemetry and uncertainty stated explicitly, so that I never mistake absent data for healthy behavior.

#### Acceptance Criteria

1. WHEN the Collector receives a Sample whose sequence number is greater than the previous sequence number plus 1 for the same Run_Identifier, THE Collector SHALL append a Gap_Record that states the first missing sequence number, the last missing sequence number, and the count of missing Samples, where the count equals the received sequence number minus the previous sequence number minus 1.
2. WHILE a Run_Identifier has no recorded termination, WHEN no Sample has been received for that Run_Identifier for longer than 3 times the Sample_Interval, THE Collector SHALL append exactly one Gap_Record per continuous silence period that states the last received Sample time and the time the silence was detected, and SHALL detect the silence within 1 Sample_Interval after the 3 times Sample_Interval threshold elapses.
3. WHEN a Sample carries a dropped-sample counter greater than 0, THE Collector SHALL append a Gap_Record that states the number of Samples the Probe reported as dropped and, where the Collector can establish it from the sequence numbers, the first and last sequence numbers of the dropped range, and where the exact dropped range cannot be established, THE Gap_Record SHALL state explicitly that the range is uncertain or unknown.
4. WHEN the Collector receives a Sample with a sequence number equal to or lower than the previous sequence number for the same Run_Identifier, THE Collector SHALL append a record that marks the Sample as a duplicate when its sequence number equals that of a previously received Sample and as a reset when its sequence number is lower than the previous sequence number and matches no previously received Sample, and SHALL keep the previously stored Sample unchanged.
5. THE Evidence_Reader SHALL report, for each Run_Identifier, the count of received Samples, the Missing_Sample_Count, the Unreconciled_Drop_Count, and the time of the last received Sample, and SHALL report the time of the last received Sample as "none received" IF the Run_Identifier has zero received Samples.
6. IF the Evidence_Store does not contain a recorded termination cause for a Run_Identifier, THEN THE Evidence_Reader SHALL state that the cause of termination is unknown.
7. WHEN the Evidence_Reader reports on a Run_Identifier that has terminated or has an open silence Gap_Record, THE Evidence_Reader SHALL state that the interval between the last received Sample and the actual termination time is unobserved.
8. THE Gap_Record SHALL contain a gap-type field whose value is exactly one of: sequence discontinuity, silence timeout, or Probe-reported drop.
9. THE Gap_Record of type sequence discontinuity SHALL carry the first and last missing sequence numbers it covers, and THE Gap_Record of type Probe-reported drop SHALL carry the Probe-reported dropped count as a count distinct from any missing sequence count, and SHALL carry the first and last sequence numbers of the range it covers only where that range is known.
10. THE Gap_Record SHALL contain a reconciliation field whose value states whether the sequence range of that Gap_Record is reconciled with, or overlaps, the sequence range of another Gap_Record for the same Run_Identifier.
11. WHERE a sequence discontinuity Gap_Record and a Probe-reported drop Gap_Record cover the same missing sequence numbers, THE Collector SHALL mark both Gap_Records as reconciled, and THE Evidence_Reader SHALL derive the Missing_Sample_Count from sequence discontinuity Gap_Records only, report each missing sequence number at most once, and SHALL NOT add the Probe-reported drop count to the Missing_Sample_Count.
12. THE Evidence_Reader SHALL report each Probe-reported drop that has no known range, whose range is uncertain, or whose range does not overlap any sequence discontinuity range, as an unreconciled drop included in the Unreconciled_Drop_Count with an uncertainty flag, separately from the Missing_Sample_Count and without folding it into the Missing_Sample_Count.
13. THE Evidence_Reader SHALL report each silence timeout Gap_Record as an interval with an unknown sample count and SHALL NOT add a silence timeout Gap_Record to the Missing_Sample_Count.

### Requirement 7: Real Measurements Only

**User Story:** As a developer, I want evidence to consist of real measurements, so that findings can be trusted.

#### Acceptance Criteria

1. WHEN the Probe takes a Sample, THE Probe SHALL read the RSS value in bytes from the operating system for the Demo_App process at the moment of that Sample.
2. WHEN the Probe takes a Sample, THE Probe SHALL measure Event_Loop_Delay in milliseconds as the difference between the scheduled time and the actual execution time of an asyncio callback, and SHALL record that difference as the Sample's Event_Loop_Delay.
3. THE Coroner SHALL contain no code path that generates synthetic, random, or pre-recorded values for any telemetry field.
4. WHEN the Experiment_Runner drives the Leak_Endpoint and a Sample is taken at least 1 second after the last request completes, THE Evidence_Store SHALL show cumulative RSS growth, measured from the last Sample taken before the first request, of at least 50 percent of the cumulative bytes retained by the Leak_Endpoint.
5. WHEN the Collector receives a Sample, THE Collector SHALL store every Sample field value exactly as received, adding only the receive time (UTC, millisecond precision) and the Run_Identifier.
6. IF the Probe cannot read RSS from the operating system or cannot measure Event_Loop_Delay for a Sample, THEN THE Probe SHALL record that field as null with a reason string as stated in Requirement 2 criterion 9, and SHALL NOT substitute any default, estimated, or previous value.
7. IF the Collector receives a Sample whose RSS or Event_Loop_Delay field is missing, or is neither numeric nor null with an accompanying reason string, THEN THE Collector SHALL reject that Sample, SHALL NOT store it as a valid Sample, and SHALL return an error indicating the invalid field, leaving previously stored Samples unchanged.

### Requirement 8: Docker Compose Environment

**User Story:** As a developer, I want a Compose environment, so that experiments can be repeated on any machine with Docker.

#### Acceptance Criteria

1. THE Coroner SHALL provide a Docker Compose file that defines the Demo_App service, the Collector service, and the Evidence_Store volume.
2. WHEN a user runs a single documented Compose command on a machine with Docker and Docker Compose installed, THE Coroner SHALL start the Demo_App and the Collector with no manual configuration and no required environment variables, and both services SHALL reach the running state within 60 seconds.
3. THE Compose file SHALL set a memory limit on the Demo_App container that is configurable through an environment variable and defaults to 128 MiB when the variable is unset or empty.
4. IF the environment variable for the Demo_App memory limit is set to a value that Docker rejects as an invalid memory size, THEN THE Compose environment SHALL fail to start the Demo_App, SHALL report an error indicating the invalid memory limit value, and SHALL leave the Collector and the Evidence_Store volume unchanged.
5. THE Compose file SHALL configure the Demo_App service with a restart policy of "no" so that, after the Demo_App process exits or is killed, the container remains stopped and its filesystem, exit code, and logs remain inspectable.
6. THE Compose file SHALL configure the Collector service with a restart policy of "on-failure" or "unless-stopped" so that the Collector container restarts after it exits with a non-zero exit code.
7. WHEN the Compose environment starts, THE Compose file SHALL configure the Demo_App service to start only after the Collector Health_Endpoint responds with HTTP 200 and storage_ok=true, THE Compose healthcheck for the Collector SHALL require both, and THE Experiment_Runner SHALL NOT begin an experiment until the Health_Endpoint has responded with HTTP 200 and storage_ok=true.
8. IF the Collector Health_Endpoint does not respond with HTTP 200 and storage_ok=true within 60 seconds of the Collector container starting, THEN THE Compose environment SHALL NOT start the Demo_App, SHALL NOT allow the Experiment_Runner to begin an experiment, and SHALL report an error indicating that the Collector is unhealthy.
9. THE Compose file SHALL pin the base image of both services to explicit version tags and SHALL NOT use the "latest" tag or an untagged image reference.
10. THE Compose file SHALL leave the Destructive_Endpoints_Flag unset for the Demo_App service by default, and SHALL enable it only when the user explicitly sets CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS=1 for a local experiment run through a documented command.
11. THE Compose file SHALL publish any host port of the Demo_App and the Collector bound only to 127.0.0.1, or publish no host port and use the internal Compose network only.
12. THE Collector SHALL not be reachable from any network interface other than the loopback interface of the host and the internal Compose network.
13. THE Compose file SHALL run the Demo_App container as non-privileged, with no host bind mounts, and with no volume mounted other than those the Demo_App requires to run.
14. IF the Health_Endpoint responds with storage_ok=false or does not include the storage_ok field before an experiment starts, THEN THE Experiment_Runner SHALL abort with a non-zero exit code and an error message stating that the Collector storage is not healthy.

### Requirement 9: Experiment A, Controlled Fatal Process Exit

**User Story:** As a developer, I want a controlled fatal-process-exit test in which the app exits immediately through os._exit(1), so that I can confirm evidence survives a process exit that skips all handlers.

#### Acceptance Criteria

1. WHEN the Experiment_Runner starts Experiment_A, THE Experiment_Runner SHALL assign a new Run_Identifier that is unique across all Run_Manifests already recorded and start the Demo_App with that identifier.
2. IF the Demo_App does not report healthy within 30 seconds of being started, THEN THE Experiment_Runner SHALL abort Experiment_A, record the failure reason in the Run_Manifest, and exit with a non-zero exit code.
3. WHEN Experiment_A is running, THE Experiment_Runner SHALL drive the Leak_Endpoint continuously for at least 5 seconds before triggering the Crash_Endpoint.
4. IF the Leak_Endpoint request fails or does not respond within 5 seconds during the drive phase, THEN THE Experiment_Runner SHALL abort Experiment_A without triggering the Crash_Endpoint, record the failure reason in the Run_Manifest, and exit with a non-zero exit code.
5. WHEN the Experiment_Runner triggers the Crash_Endpoint, THE Experiment_Runner SHALL record the crash request time in the Run_Manifest.
6. WHEN the Demo_App process terminates after the Crash_Endpoint is called, THE Experiment_Runner SHALL read the Container_State and record the exit code in the Run_Manifest, where the expected exit code is 1.
7. IF the Demo_App process does not terminate within 30 seconds of the crash request time, THEN THE Experiment_Runner SHALL record a timeout failure in the Run_Manifest and exit with a non-zero exit code.
8. WHEN Experiment_A completes, THE Collector SHALL remain running as verified by a response from its Health_Endpoint within 5 seconds with HTTP 200 and storage_ok=true, and THE Experiment_Runner SHALL mark Experiment_A as failed if that response is not received.
9. WHEN Experiment_A completes, THE Evidence_Store SHALL contain at least one Sample for the Run_Identifier with a timestamp earlier than the crash request time.
10. THE Run_Manifest for Experiment_A SHALL record that the Container_State OOMKilled flag is false.
11. IF the recorded exit code in Experiment_A is not 1, THEN THE Experiment_Runner SHALL mark Experiment_A as failed in the Run_Manifest and indicate the unexpected exit code to the developer.
12. THE Coroner SHALL describe the Crash_Endpoint as a controlled fatal-process-exit (os._exit(1)) and SHALL NOT describe it as a Python exception or an unhandled exception in code comments, documentation, the Run_Manifest, or the summary output.

### Requirement 10: Experiment B, SIGKILL

**User Story:** As a developer, I want to SIGKILL the app, so that I can prove evidence survives a termination in which no handler can run.

#### Acceptance Criteria

1. WHEN the Experiment_Runner starts Experiment_B, THE Experiment_Runner SHALL assign a new Run_Identifier that differs from every Run_Identifier recorded in previous Run_Manifests, and start the Demo_App with that identifier.
2. WHEN Experiment_B is running, THE Experiment_Runner SHALL drive the Leak_Endpoint continuously for at least 5 seconds before sending SIGKILL to the Demo_App container.
3. WHEN the Experiment_Runner sends SIGKILL, THE Experiment_Runner SHALL record the send time in UTC with millisecond precision in the Run_Manifest.
4. WHEN the Demo_App container stops, THE Experiment_Runner SHALL read the Container_State within 10 seconds and record the exit code in the Run_Manifest, where the expected exit code is 137.
5. IF the Demo_App container has not stopped within 10 seconds after SIGKILL is sent, THEN THE Experiment_Runner SHALL mark Experiment_B as failed in the Run_Manifest and indicate the timeout to the developer.
6. IF the recorded exit code is not 137, THEN THE Experiment_Runner SHALL mark Experiment_B as failed in the Run_Manifest and indicate the unexpected exit code to the developer.
7. WHEN Experiment_B completes, THE Collector SHALL respond on its Health_Endpoint within 5 seconds of the health check request with HTTP 200 and storage_ok=true, confirming it remained running with healthy persistence, and THE Experiment_Runner SHALL mark Experiment_B as failed if that response is not received.
8. WHEN Experiment_B completes, THE Evidence_Store SHALL contain at least one Sample for the Run_Identifier with a Capture_Time earlier than the SIGKILL send time.
9. THE Run_Manifest for Experiment_B SHALL record that the Container_State OOMKilled flag is false.
10. THE Coroner SHALL preserve the Experiment_B evidence without any record written by an application shutdown handler, and THE Collector SHALL store both the Capture_Time and the Receive_Time in each stored Sample record.
11. WHERE a Sample has a Capture_Time earlier than the SIGKILL send time and a Receive_Time later than the SIGKILL send time, THE Experiment_Runner SHALL accept the Sample as valid evidence and SHALL report the number of such Samples in the Run_Manifest and the summary.
12. IF a Sample for the Run_Identifier has a Capture_Time later than the SIGKILL send time plus a documented clock-difference tolerance, THEN THE Experiment_Runner SHALL mark Experiment_B as failed and report the Sample as an anomaly, where the tolerance is documented and is no more than 250 milliseconds.
13. IF a Sample for the Run_Identifier has a Capture_Time later than the time at which the container stopped, as recorded by Docker inspection, THEN THE Experiment_Runner SHALL report that Sample as an anomaly in the Run_Manifest and the summary, because the Experiment_Runner and the Docker host share one clock.

### Requirement 11: Experiment C, Genuine Docker OOM Kill

**User Story:** As a developer, I want a real Docker OOM kill, so that evidence reflects the actual production failure mode.

#### Acceptance Criteria

1. WHEN the Experiment_Runner starts Experiment_C, THE Experiment_Runner SHALL assign a new Run_Identifier that differs from every Run_Identifier of previous runs and start the Demo_App container with that identifier and the configured container memory limit, after verifying that the Collector Health_Endpoint reports HTTP 200 and storage_ok=true.
2. IF the Demo_App container is not running within 30 seconds of Experiment_C starting, THEN THE Experiment_Runner SHALL mark Experiment_C as failed, stop calling the Leak_Endpoint, and report that the Demo_App failed to start.
3. WHILE Experiment_C is running and the Demo_App container is running, THE Experiment_Runner SHALL call the Leak_Endpoint at intervals of no more than 1 second until the Demo_App container stops or 120 seconds elapse from the first Leak_Endpoint call.
4. WHEN the Demo_App container stops, THE Experiment_Runner SHALL read the Container_State through Docker inspection within 5 seconds.
5. WHEN the Experiment_Runner has read the Container_State, THE Experiment_Runner SHALL record the OOMKilled flag, exit code, and container status from Container_State in the Run_Manifest, where the expected exit code is 137 and the expected OOMKilled flag is true.
6. IF the Container_State OOMKilled flag is not true after the Demo_App container stops, THEN THE Experiment_Runner SHALL mark Experiment_C as failed, report the observed Container_State, and retain the Run_Manifest.
7. IF 120 seconds elapse from the first Leak_Endpoint call and the Demo_App container is still running, THEN THE Experiment_Runner SHALL stop calling the Leak_Endpoint, mark Experiment_C as failed, and report that the timeout elapsed.
8. IF Docker inspection fails or returns no Container_State, THEN THE Experiment_Runner SHALL mark Experiment_C as failed and report that Container_State could not be read.
9. THE Experiment_Runner SHALL not use a SIGKILL sent by the Experiment_Runner, a crash endpoint call, or a simulated exit code as the cause of termination in Experiment_C.
10. WHEN Experiment_C completes, THE Experiment_Runner SHALL request the Collector Health_Endpoint and confirm a response within 5 seconds with HTTP 200 and storage_ok=true (Collector survival), and SHALL mark Experiment_C as failed if no such response is received.
11. WHEN Experiment_C completes, THE Evidence_Store SHALL contain at least 1 Sample for the Run_Identifier received by the Collector before the Demo_App container stopped (preserved pre-crash evidence).
12. WHEN Experiment_C completes, THE Experiment_Runner SHALL record the configured memory limit in bytes and the highest RSS value in the Evidence_Store in bytes in the Run_Manifest.
13. WHEN the Experiment_Runner reads the Container_State, THE Experiment_Runner SHALL record in the Run_Manifest the following values taken from `docker inspect` of the Demo_App container: State.OOMKilled, State.ExitCode, State.Status, the full 64-character container ID, and HostConfig.Memory in bytes.
14. WHEN the Experiment_Runner reads the Container_State, THE Experiment_Runner SHALL save the raw `docker inspect` output (or the relevant raw excerpt containing the fields in criterion 13) with the Run_Manifest and SHALL record the inspect time in UTC with millisecond precision in the Run_Manifest.
15. THE Experiment_C integration test SHALL assert that the State.OOMKilled, State.ExitCode, State.Status, container ID, and HostConfig.Memory values in the Run_Manifest are equal to the values obtained from Docker inspection, and SHALL assert that HostConfig.Memory equals the configured memory limit in bytes.
16. WHERE the Probe Samples for the Run_Identifier report a container identifier, THE Experiment_C integration test SHALL assert that the container ID in the Run_Manifest matches the container identifier reported by those Samples.
17. THE Experiment_Runner SHALL determine the pass or fail outcome of Experiment_C only from the following mandatory conditions: State.OOMKilled equal to true, State.ExitCode equal to 137, Collector survival as stated in criterion 10, preserved pre-crash evidence as stated in criterion 11, measurable memory growth as stated in criterion 18, and the Docker inspection evidence recording of criteria 13 and 14.
18. WHEN Experiment_C completes, THE Experiment_Runner SHALL verify that the RSS value of the peak Sample (the Sample with the highest RSS) exceeds the RSS value of the first Sample for the Run_Identifier by a positive amount (measurable growth), and SHALL mark Experiment_C as failed if the evidence contains no such positive increase.
19. WHEN Experiment_C completes, THE Experiment_Runner SHALL compute the Diagnostic_Metrics rss_monotonic_tail (whether the RSS values of the last 5 Samples, or of all Samples where fewer than 5 exist, are non-decreasing in Capture_Time order), peak_rss_ratio_of_limit (highest RSS divided by the configured memory limit), and the Sample count, and SHALL record them in the Run_Manifest without using them to determine the pass or fail outcome.
20. WHEN Experiment_C completes, THE Experiment_Runner SHALL record in the Run_Manifest and print in the summary the Sampling_Limitations, including the Sample_Interval relative to the leak speed, that the final seconds before the out-of-memory kill may be unobserved, and that the peak RSS may understate the memory limit.

### Requirement 12: Repeatability and Inspectability

**User Story:** As a developer, I want repeated experiments to produce consistent, inspectable evidence, so that results can be compared and trusted.

#### Acceptance Criteria

1. WHEN the Experiment_Runner executes the same experiment more than once, THE Experiment_Runner SHALL create a separate Evidence_Store file and a separate Run_Manifest for each run, each associated with a unique Run_Identifier, and SHALL leave the Evidence_Store files and Run_Manifests of all earlier runs unmodified.
2. THE Experiment_Runner SHALL write every record in the Evidence_Store for all three experiments using the same JSONL record schema, such that each line is a single valid JSON object and the set of required fields is identical across the three experiments.
3. THE Evidence_Store SHALL be a UTF-8 text file with one JSON record per line, readable with standard text tools (such as `cat`, `grep`, and `wc -l`) without a Coroner-specific viewer.
4. WHEN the same experiment is executed three consecutive times, THE Experiment_Runner SHALL produce three Run_Manifests in which the observed Container_State exit code and the OOMKilled flag are each identical across all three runs.
5. WHEN a user invokes the summary command with a Run_Identifier, THE Experiment_Runner SHALL use the Evidence_Reader to print a summary containing the sample count, gap count, last sample time, and Container_State for that run.
6. IF the summary command is invoked with a Run_Identifier for which no Run_Manifest or Evidence_Store exists, or whose Evidence_Store cannot be parsed, THEN THE Experiment_Runner SHALL print an error message indicating the cause, SHALL exit with a non-zero exit code, and SHALL NOT modify any stored run data.
7. IF an experiment fails at any point after the Run_Identifier is assigned, THEN THE Experiment_Runner SHALL still write the Run_Manifest, recording the failure and any Container_State observed up to the failure, and SHALL retain the Evidence_Store records written before the failure.

### Requirement 13: Automated Tests

**User Story:** As a developer, I want automated tests, so that I can verify M0 behavior reliably and repeatedly.

#### Acceptance Criteria

1. THE Coroner SHALL provide unit tests written with pytest for each of the following components: Probe sampling, Event_Loop_Delay measurement, Evidence_Writer, Evidence_Reader, and gap detection, with at least one passing test per component.
2. THE Coroner SHALL provide a pytest test that verifies the round-trip property that writing a sequence of 0 to 1,000 records with the Evidence_Writer and reading them back with the Evidence_Reader yields the same number of records, in the same order, with every field equal to the original.
3. THE Coroner SHALL provide a pytest test that verifies gap detection for a sequence of Samples containing at least one missing sequence number, at least one duplicated sequence number, and at least one reset sequence number (a sequence number lower than its predecessor), and that asserts each of the three anomalies is reported separately.
4. THE Coroner SHALL provide pytest Docker integration tests that run Experiment_A, Experiment_B, and Experiment_C each against the Docker Compose environment, with one test per experiment, kept in a directory separate from the unit tests and marked with the pytest marker `docker_integration`.
5. WHEN the Experiment_C integration test completes, THE Experiment_C integration test SHALL assert that the OOMKilled flag obtained from Docker inspection of the stopped Demo_App container is true, the exit code is 137, Collector survival with storage_ok=true, at least 1 preserved pre-crash Sample, measurable memory growth, the Docker inspection values stated in Requirement 11 criteria 13 to 16, and that the Diagnostic_Metrics and Sampling_Limitations are present in the Run_Manifest without having determined the pass or fail outcome.
6. IF the user opts in to the Docker integration tests and Docker is not available on the machine, THEN THE Coroner SHALL report each Docker integration test as skipped or failed with a message stating that Docker is unavailable, AND THE Coroner SHALL still run all unit tests.
7. WHEN a user runs the documented pytest command, THE Coroner SHALL report a pass, fail, or skip outcome for every collected test, including the total count of each outcome.
8. THE Coroner SHALL run all unit tests to completion with Docker stopped or uninstalled and with no network connectivity, and no unit test SHALL open a network connection or invoke the Docker CLI or Docker API.
9. IF any integration test that was opted in fails, THEN THE Coroner SHALL report a non-zero exit status from the documented pytest command, AND THE Coroner SHALL leave the unit test results unaffected.
10. WHEN a user runs the default `pytest` invocation without opting in, THE Coroner SHALL run only the Docker-free, network-free unit tests and SHALL NOT run any test marked `docker_integration`.
11. WHERE the user opts in explicitly through the `--run-docker` option or the environment variable CORONER_RUN_DOCKER=1, THE Coroner SHALL run the Docker integration tests for Experiment_A, Experiment_B, and Experiment_C in addition to the unit tests.
12. THE Coroner SHALL provide a pytest test that verifies the reconciliation rule of Requirement 6: for a sequence discontinuity Gap_Record and a Probe-reported drop Gap_Record that cover the same missing sequence numbers, the Evidence_Reader reports each missing sequence number once, the Missing_Sample_Count equals the size of the union of the sequence discontinuity ranges only, each missing sequence number is reported at most once, a Probe-reported drop is never added to the Missing_Sample_Count, a Probe-reported drop with an unknown or uncertain range is reported in the Unreconciled_Drop_Count with an uncertainty flag, and a silence timeout Gap_Record adds nothing to the Missing_Sample_Count.
13. THE Coroner SHALL provide pytest unit tests that verify the repair of an incomplete last line: the original fragment bytes are preserved in a Repair_Record containing the byte length, file name, and line number, the repair is persisted before the next append, new records start on a fresh line, and the Evidence_Reader reports the Malformed_Line with its file, line number, and byte length, excludes it from the Sample counts, and continues parsing.
14. THE Coroner SHALL provide pytest unit tests that verify the Durability_Level selection of Requirement 4: the default is Crash_Durable, an invalid CORONER_DURABILITY value is rejected, and the Crash_Durable level performs flush and fsync before acknowledgement (verified without a real host crash, using a test double for the fsync call).
15. THE Coroner SHALL provide a non-destructive pytest test, requiring no container kill, that verifies an ordinary HTTP 500 does not terminate the service: after a request to the Error_Endpoint returns HTTP 500, the Demo_App process is still running, the Demo_App container is still running, the health endpoint returns HTTP 200, and the Probe keeps sampling with the same process identifier and an increasing sequence number.
16. THE Coroner SHALL provide Docker-free pytest unit tests that verify: the Experiment_B Sample timing classification (a Sample with Capture_Time before and Receive_Time after the SIGKILL send time is accepted and counted, and a Sample with Capture_Time after the SIGKILL send time plus the tolerance is reported as an anomaly), the Experiment_C pass and fail evaluation (Diagnostic_Metrics never change the outcome, and the absence of measurable growth fails the experiment), and the abort of the Experiment_Runner when the Health_Endpoint reports storage_ok=false.
17. THE Coroner SHALL provide a pytest test that verifies that, with the Destructive_Endpoints_Flag unset, requests to the Crash_Endpoint, the Leak_Endpoint, and the Error_Endpoint each return HTTP 404 and the process keeps running.

### Requirement 14: Reproduction Instructions and Modularity

**User Story:** As a hackathon reviewer, I want clear reproduction steps and a small modular codebase, so that I can verify the results myself.

#### Acceptance Criteria

1. THE Coroner SHALL provide documentation that lists prerequisites (including required tool names and minimum versions), the commands to start the Compose environment, the commands to run each experiment, and the commands to inspect evidence.
2. THE documentation SHALL state, for each experiment, the expected exit code and the expected OOMKilled flag value (true or false): Experiment_A exit code 1 with OOMKilled false, Experiment_B exit code 137 with OOMKilled false, and Experiment_C exit code 137 with OOMKilled true.
3. THE documentation SHALL state the known limitations of M0, including that the interval between the last Sample and the termination is unobserved, that Experiment_C Diagnostic_Metrics are informational and do not determine pass or fail, and the Sampling_Limitations of Experiment_C.
4. THE Coroner SHALL separate the Demo_App, Probe, Collector, Evidence_Store, and Experiment_Runner into distinct Python packages or modules, such that importing any one of them in a fresh interpreter completes without raising an ImportError caused by a circular import.
5. THE Coroner SHALL list every third-party Python dependency in a requirements file with an exact version pin (for example, `name==X.Y.Z`) and no version ranges.
6. THE Coroner SHALL implement the Probe in no more than 300 lines of Python code, counting only non-blank lines and excluding test files.
7. WHEN a reviewer follows the documented commands in order on a machine that meets the listed prerequisites, THE Coroner SHALL produce, for each experiment, an exit code and OOMKilled flag matching the values stated in the documentation.
8. THE documentation SHALL state that the default pytest command runs only Docker-free, network-free unit tests, and SHALL state the commands to opt in to the Docker integration tests (`--run-docker` or CORONER_RUN_DOCKER=1) and the Docker prerequisites for them.
9. THE documentation SHALL state the two Durability_Levels, the M0 default Crash_Durable, the CORONER_DURABILITY variable, and the limitation that fsync guarantees depend on the Docker volume and the underlying host filesystem and storage.
10. THE documentation SHALL state that Experiment_A is a controlled fatal-process-exit test using os._exit(1), and SHALL state that the Destructive_Endpoints are disabled unless CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS=1 is set, are for local testing only, and must not be exposed beyond localhost.
11. THE documentation SHALL state that the Collector Health_Endpoint body includes storage_ok, that HTTP 200 alone does not prove healthy persistence, and that the Experiment_Runner requires storage_ok=true before and after each experiment.
12. THE documentation SHALL state that an Experiment_B Sample may have a Capture_Time before the SIGKILL send time and a Receive_Time after it, and SHALL state the documented clock-difference tolerance.

### Requirement 15: Ordinary HTTP 500 Does Not Terminate the Service

**User Story:** As a developer, I want proof that an ordinary application error returns HTTP 500 without killing the service, so that I do not confuse an ordinary exception with a fatal process exit.

#### Acceptance Criteria

1. WHEN a client sends a request to the Error_Endpoint, THE Demo_App SHALL return an HTTP 500 response.
2. WHEN the Error_Endpoint has returned an HTTP 500 response, THE Demo_App process SHALL remain running with the same process identifier.
3. WHEN the Error_Endpoint has returned an HTTP 500 response, THE Demo_App container SHALL remain in the running state.
4. WHEN the Error_Endpoint has returned an HTTP 500 response, THE Demo_App SHALL return an HTTP 200 response from the health endpoint within 1 second.
5. WHEN the Error_Endpoint has returned an HTTP 500 response, THE Probe SHALL continue to produce Samples with the same process identifier and with sequence numbers that increase by 1 per Sample.
6. THE Error_Endpoint SHALL be available only while the Destructive_Endpoints_Flag is set to 1, as stated in Requirement 1 criterion 10.

### Requirement 16: Windows Compatibility of the Experiment_Runner

**User Story:** As a developer on Windows, I want to run the experiments from PowerShell with Docker Desktop, so that I can reproduce M0 without a POSIX shell.

#### Acceptance Criteria

1. THE Experiment_Runner SHALL run on Windows PowerShell with Docker Desktop using Linux containers, and on POSIX shells, with identical experiment behavior and Run_Manifest content apart from file paths.
2. WHEN the Experiment_Runner invokes Docker or Docker Compose, THE Experiment_Runner SHALL pass Compose environment variables through the env argument of subprocess.run as a merged copy of os.environ, and SHALL NOT rely on POSIX inline assignment of the form VAR=value command.
3. THE Experiment_Runner SHALL construct all file system paths with pathlib.
4. THE documentation SHALL include a Windows setup and verification section that states the Docker Desktop requirement with Linux containers and WSL2, the minimum Python version, the verification commands in PowerShell, and a note on how to confirm that the Demo_App memory limit is enforced.
5. THE Demo_App, the Probe, and the Collector SHALL run only inside Linux containers, and THE Coroner SHALL NOT require container-side code to support Windows.
