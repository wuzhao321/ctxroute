# Request completion and optional action recovery

The model gateway can use streamed Chat Completions by setting `stream=True`
with the PydanticAI transport. The default remains non-streaming. Streaming
changes the transport and must be included in an experiment's frozen controls.
Model, messages, thinking settings, output limit, temperature and output format
are otherwise preserved. The provider can still interrupt a stream; streaming
is not a delivery guarantee.

The receiver requires the SSE `[DONE]` marker, validated original usage and an
explicit, consistent completion reason before returning model text to the
gateway. Only `stop` can reach the engine's action-schema validation. It handles
keep-alive comments, LF/CRLF/CR line endings, split events and compressed HTTP
responses. Partial text is never executed.
The original response content is kept out of transfer diagnostics, including
reasoning text. A response is bounded to 16 MiB of decoded bytes and an SSE
event to 1 MiB; the whole SDK operation also has a deadline that heartbeats
cannot extend. The SDK sends one HTTP request and does not retry internally.

Both successful and failed request events record available HTTP status,
sanitized provider request identifier, byte/chunk counts and times to headers,
first byte and last byte. Streaming records also contain event and keep-alive
counts and whether the completion marker arrived. These observations locate
the stage of an interruption, but do not identify which network node caused it.

The stream observer also retains a small `stream_terminal` receipt: an allowed
finish-reason value, validated numeric usage, and booleans indicating whether
answer text or reasoning was present. It does not retain either text. Missing,
invalid and conflicting usage remain distinct. The receipt survives SDK output
validation failures, including a response containing only reasoning. This lets
diagnostics distinguish output exhaustion (`length`) from an empty answer with
`stop`, which can otherwise produce the same SDK exception type. Unknown finish
reasons are reduced to an enum rather than copied into logs.

The receipt also reports `receipt_status` independently of SDK acceptance:
`incomplete`, `invalid_terminal`, `invalid_usage`, or `valid`. A valid receipt
may still report `length` and cannot authorize an action. Optional
`reasoning_tokens` is retained only as a nonnegative integer no larger than
validated completion tokens. Missing, invalid and conflicting detail remains
unknown with an explicit status; it is never inferred from SSE event counts.
This detail is already included in completion usage and does not add a charge
or change the five required billing counts or their settlement rules.

The receipt freezes at the first `[DONE]`, matching the SDK's parsing boundary.
It is also an acceptance gate: an SDK default `stop` cannot replace a missing
wire reason, and normalized SDK usage must equal the validated wire counts.
Repeated usage that the SDK accumulates differently is rejected with its
reservation retained. This prevents invalid totals, conflicting usage or
conflicting termination from being hidden by SDK normalization.

The receipt never bypasses SDK validation or settles a reservation after SDK
failure. `content_present` means a nonempty wire content delta, which may still
be whitespace or invalid output. `sdk_output_accepted` separately records whether
the SDK returned output; neither field means an action passed schema validation.
`[DONE]` is not acceptance. `response_completed` describes HTTP iterator
exhaustion, which an SDK may skip after receiving `[DONE]`.

Failure diagnostics traverse exception-group leaves as well as exception causes,
retaining a bounded list of exception types without arbitrary exception messages.
Transport errors take precedence over terminal interpretation. On a completed
stream, the classifications are:

| Evidence | `failure_kind` |
|---|---|
| Missing, unknown or conflicting terminal metadata | `invalid_stream` |
| Explicit `length` | `output_exhausted` |
| Other explicit non-`stop` reason | `model_terminated` |
| `stop` with no nonempty wire content | `empty_answer` |
| Missing/invalid/conflicting usage, or SDK/wire count mismatch | `invalid_usage` |
| SDK rejects otherwise valid terminal evidence | `sdk_output_rejected` |

`length` does not establish which token/context limit was reached or whether
thinking consumed the limit. Secondary facts remain in `stream_terminal`, even
when a primary failure classification is selected. Valid usage returned by the
SDK on a partial `length` answer is accounted before the gateway rejects the
answer, preserving existing settlement behavior. Missing or uncertain usage
and SDK output rejection retain the full reservation. Invalid actions after a
valid `stop` remain engine `format_error`/`format_failure` events.

## Bounded retry

`Engine(..., model_attempts=2, retry_transient_only=True)` allows one further
attempt after a connection interruption, timeout, incomplete stream, HTTP 429,
or selected HTTP 5xx responses. Definitive errors and invalid usage are not
retried in this mode. The same step and unmodified context are used; commands
are executed only after a complete response. No engine checkpoint is resumed.

Each attempt has a new request ID and must reserve its own worst-case charge.
The interrupted request's unknown charge stays reserved. A retry cannot bypass
the run/global budget or remaining wall time. A provider may have billed an
interrupted request; this is not exactly-once generation. Ordinary command
failures are not replayed by this recovery mechanism.

## Optional metadata recovery

`Engine(..., recover_action_metadata=True)` enables two narrow protocol rules:

- An otherwise valid action followed by an exact echo of the current
  `NEXT ACTION` metadata can be parsed after removing only that suffix. A changed
  suffix, another action, Markdown fences, extra braces or arbitrary trailing
  text remain invalid. The original model response and normalization flag stay
  in the trace.
- A valid action with unsupported optional `remember` quotes can execute after
  all its proposed memory notes are rejected. Existing verified memory is
  retained; invalid notes never enter fixed memory. The validated command is
  unchanged, and the rejection is recorded. Invalid action schemas still fail.

These options change action acceptance and retry policy. They are opt-in,
independent of compression/routing, and require a new frozen experiment when
compared with the strict default. They do not improve a model's choice of
source files, force edits, or imply benchmark success.

## Action history and corrective feedback

Model-visible history represents each action in the same protocol accepted by
`Action.parse`: bash has `tool` and `command`, submit has `tool`, and recall has
`tool`, `id`, `start`, and `end`. Optional `remember` notes retain their protocol
shape. Internal `kind`, `artifact_id`, and irrelevant default fields are not
rendered inside actions. Compression input uses the same representation. Raw
state snapshots retain their original internal representation for audit readers.

Trailing JSON content and unsupported fields still fail validation. Feedback
identifies the specific problem and states that the rejected action was not
executed. It asks for a newly generated valid action; it does not trim arbitrary
trailing characters, remove unsupported fields, replay an earlier command, or
increase the existing consecutive-format-error limit. Request costs and steps
remain accounted for when an action is rejected. Improving representation and
feedback does not guarantee that a model will generate valid JSON.

## Verification

Offline tests use a synthetic key, blocked sockets and in-memory HTTP streams
through the installed SDKs. They cover split UTF-8, gzip, keep-alives,
disconnection, absent completion markers, missing usage, total deadlines,
retained reservations, and budget rejection before a retry. Engine fixtures
check that partial/extra actions never execute and rejected memory is not
promoted. Thinking-only streams also verify that their terminal receipt survives
SDK rejection, their reservations remain held, and no command or retry runs.
Receipt parity tests cover absent/conflicting finish reasons, malformed and
duplicate usage, SDK rejection after completion, nested network exceptions,
all three line endings and chunk-independent handling of bytes after `[DONE]`.
Official benchmark grading remains an independent process.

Protocol references: [DeepSeek keep-alive behavior](https://api-docs.deepseek.com/quick_start/rate_limit/)
and [Chat Completions streaming schema](https://api-docs.deepseek.com/api/create-chat-completion/).
