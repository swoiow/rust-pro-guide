# Rust Engineering Rules (AGENTS.md)

**Scope:** Rust code generation, review, refactoring, and optimization across CPU, memory, and networking.
**Priority:** Correctness and project contracts first; optimize measured bottlenecks without introducing unnecessary complexity.

## Engineering Foundations

### Rule-1 — Respect the task contract
Identify the required behavior, target platform, Rust edition, permitted dependencies, and constraints before changing code. Do not invent requirements or expand the requested scope.

### Rule-2 — Prefer minimal, compatible changes
Make the smallest coherent modification that satisfies the task and preserves existing public behavior. Avoid unrelated refactors, new abstractions, and dependencies without a clear benefit.

### Rule-3 — Design ownership explicitly
Determine who owns, borrows, mutates, and releases each value. Prefer safe borrowing when it fits the actual lifetime and ownership requirements.

### Rule-4 — Define precise interfaces
Use clear public types, error contracts, and ownership semantics; allow normal type inference inside implementations. Introduce generic parameters and explicit lifetimes only when they express a real constraint.

### Rule-5 — Prefer measured tradeoffs to universal thresholds
Treat concurrency limits, task sizes, thread counts, buffer sizes, and parallelization thresholds as workload-specific tuning parameters. Do not encode arbitrary constants as universal performance rules.

## CPU, Async, and Concurrency

### Rule-6 — Classify the workload
Distinguish I/O waits, CPU-intensive work, and synchronization contention before choosing an execution strategy. Measure mixed workloads rather than classifying them by function name alone.

### Rule-7 — Keep the async runtime responsive
Use Tokio for async I/O in projects that already use Tokio, and avoid blocking its worker threads with long synchronous work. An `async` function is not automatically non-blocking.

### Rule-8 — Offload substantial synchronous computation
Use a dedicated blocking execution path when synchronous work would harm async responsiveness. `tokio::task::spawn_blocking` uses Tokio's blocking pool and does not automatically execute work in Rayon.

### Rule-9 — Use Rayon for appropriate data parallelism
Choose Rayon when independent CPU-heavy operations measurably benefit from multicore execution. Make Tokio–Rayon boundaries explicit and avoid unnecessary nested scheduling.

### Rule-10 — Avoid trivial task spawning
Run cheap, predictable operations inline when scheduling overhead would outweigh offloading. Check latency impact before retaining CPU work on async worker threads.

### Rule-11 — Batch small CPU operations
Group short operations when per-item scheduling cost dominates useful computation. Select batch sizes according to item cost, latency, fairness, and memory footprint.

### Rule-12 — Apply conditional parallelism
Use serial processing for workloads too small to benefit from parallel execution. Benchmark the crossover using representative input sizes, per-item work, and actual hardware.

### Rule-13 — Bound concurrent work and queues
Apply admission control and bounded queues to requests, tasks, and blocking jobs. A semaphore placed after an unbounded enqueue does not prevent memory growth.

### Rule-14 — Size execution pools for deployment limits
Account for physical or logical CPUs, cgroup quotas, competing pools, and blocking behavior when configuring workers. Avoid fixed global caps and oversubscription without benchmark evidence.

### Rule-15 — Prefer local aggregation
Keep intermediate counters and accumulators task-local or thread-local when possible, then merge results at a defined boundary. Synchronize genuinely shared mutations correctly.

### Rule-16 — Investigate cache contention before padding
Measure false sharing, synchronization contention, and poor cache locality before changing alignment or layout. Consider the extra memory cost and platform differences.

### Rule-17 — Handle task failures and cancellation
Propagate join errors and task failures rather than silently dropping them. Cancellation of an awaiting future does not necessarily stop underlying synchronous work, so give long-running jobs explicit stop semantics.

## Memory and Allocation

### Rule-18 — Choose storage by ownership and size
Use local values for naturally local data and heap-backed storage when runtime size or ownership requires it. Do not assume that `Vec<T>` stores its elements on the stack.

### Rule-19 — Prefer borrowing for read-only processing
Accept references, string slices, or byte slices when a function does not need ownership. Return owned data only when the caller must retain it beyond a valid borrowing lifetime.

### Rule-20 — Avoid unnecessary deep copies
Review `clone()`, `to_owned()`, and `to_string()` in frequently executed paths. Preserve copies when necessary for correctness or when simpler non-critical code provides a better tradeoff.

### Rule-21 — Describe zero-copy accurately
Reuse borrowed views and shared byte buffers when ownership and lifetime boundaries permit. Never label a copying conversion as zero-copy.

### Rule-22 — Reuse temporary buffers thoughtfully
Reuse suitably scoped mutable buffers when repeated allocation is a measured cost. Do not retain oversized or sensitive buffers longer than necessary.

### Rule-23 — Select string types intentionally
Prefer `&str` for borrowed text, `Cow<'_, str>` for conditional ownership, and `String` for owned or mutable text. Choose formatting or capacity reservation based on actual allocation behavior.

### Rule-24 — Preallocate only with justified bounds
Reserve collection capacity when expected sizes are reliable and memory costs are acceptable. Validate externally supplied size hints before allocation.

### Rule-25 — Bound input-driven memory growth
Enforce limits on frames, messages, payloads, and collections before untrusted data can cause uncontrolled allocation. Define clear behavior when a limit is exceeded.

### Rule-26 — Use smart pointers according to semantics
Choose `Box`, `Rc`, and `Arc` for their actual ownership and sharing properties. Add `Mutex` or `RwLock` only where synchronized mutation is required, and avoid strong-reference cycles.

### Rule-27 — Minimize lock scope
Keep critical sections short and avoid holding unrelated locks across `.await`. Select synchronous or async-aware locks based on how the lock is used, not solely on whether the surrounding function is async.

### Rule-28 — Optimize layout only when proven valuable
Measure structure size, alignment, and locality before changing field layout or representation. Preserve ABI constraints and do not assume all systems share identical cache-line properties.

### Rule-29 — Use arena allocation selectively
Consider arenas for groups of short-lived objects with a common lifetime. Evaluate destructor semantics, peak retained memory, ownership constraints, and dependency costs first.

### Rule-30 — Profile allocations and retained memory
Use representative profiling or benchmarks to identify allocation hot paths and memory retention. Distinguish measured improvements from code-level hypotheses.

## Network and I/O

### Rule-31 — Reuse connections when valid
Prefer persistent connections or bounded pools for repeated operations when the protocol and remote service permit reuse. Tune pool limits using workload behavior, backend capacity, and queueing.

### Rule-32 — Reuse configured HTTP clients
Share compatible HTTP clients to benefit from connection pooling and keep-alive. Enable protocol-specific optimizations only when the peer and deployment support them.

### Rule-33 — Parse network streams incrementally
Handle partial reads, framing, incomplete input, end-of-stream, and malformed messages according to protocol semantics. Never assume one socket read yields a complete message.

### Rule-34 — Set meaningful network deadlines
Define connection, read, write, idle, and overall timeouts according to service requirements. Distinguish timeouts from transport errors, protocol failures, and cancellation.

### Rule-35 — Batch network work selectively
Group compatible requests to reduce round trips when supported by the protocol and remote service. Respect payload size, failure isolation, memory use, and tail-latency tradeoffs.

### Rule-36 — Enforce end-to-end backpressure
Bound ingress, channels, write buffers, and work queues wherever producers may outrun consumers. Define explicit behavior for overload, rejection, and shutdown.

### Rule-37 — Tune transport options from evidence
Adjust keepalive, Nagle-related settings, backlog, and other socket options only for documented workload needs and supported platforms. Preserve file-descriptor ownership and avoid unsafe system-wide tuning by default.

### Rule-38 — Right-size socket and application buffers
Choose buffering based on frame limits, throughput, bandwidth-delay behavior, and total concurrent memory budget. Validate settings under realistic load instead of applying universal ratios.

### Rule-39 — Manage resources and shutdown safely
Use RAII for ordinary cleanup and define lifecycle behavior for tasks, sockets, pools, and pending requests. Graceful shutdown should stop new admission, address in-flight operations, and release resources within explicit constraints.

## Reliability and Delivery

### Rule-40 — Return contextual errors
Use `Result<T, E>` for recoverable failures and structured variants when callers must distinguish error classes. Attach useful context at failure boundaries without masking the original cause.

### Rule-41 — Restrict unsafe operations
Prefer safe Rust and established APIs; use `unsafe` only for a specific necessary capability. Document safety invariants and check pointer, aliasing, lifetime, and ownership obligations.

### Rule-42 — Document contracts, not obvious implementation
Keep public API documentation focused on purpose, inputs, outputs, failure modes, and non-obvious invariants. Avoid commentary that simply restates the code.

### Rule-43 — Test boundaries and preserve semantics
Cover normal operations and relevant failures, including malformed input, resource limits, timeout, cancellation, and shutdown. Verify ordering and compatibility requirements when applying optimizations.

### Rule-44 — Verify before claiming completion
Run formatting, compilation, relevant lints, tests, and benchmarks when the environment permits. Explicitly distinguish checks performed from checks not run.

### Rule-45 — Keep delivery explanations concise
Report the change, decisive tradeoffs, and any unresolved risk directly. Limit each explanatory point to one to three sentences, without examples unless requested.
