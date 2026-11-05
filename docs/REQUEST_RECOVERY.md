# Request completion and optional action recovery

The model gateway can use streamed Chat Completions by setting `stream=True`
with the PydanticAI transport. The default remains non-streaming. Streaming
changes the transport and must be included in an experiment's frozen controls.
Model, messages, thinking settings, output limit, temperature and output format
are otherwise preserved. The provider can still interrupt a stream; streaming
is not a delivery guarantee.

The receiver waits for the SSE `[DONE]` marker, final usage and the model's
completion reason before returning an action. It handles keep-alive comments,
split events and compressed HTTP responses. Partial text is never executed.
The original response content is kept out of transfer diagnostics, including
reasoning text. A response is bounded to 16 MiB of decoded bytes and an SSE
event to 1 MiB; the whole SDK operation also has a deadline that heartbeats
cannot extend. The SDK sends one HTTP request and does not retry internally.

Both successful and failed request events record available HTTP status,
sanitized provider request identifier, byte/chunk counts and times to headers,
first byte and last byte. Streaming records also contain event and keep-alive
counts and whether the completion marker arrived. These observations locate
the stage of an interruption, but do not identify which network node caused it.

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

## Verification

Offline tests use a synthetic key, blocked sockets and in-memory HTTP streams
through the installed SDKs. They cover split UTF-8, gzip, keep-alives,
disconnection, absent completion markers, missing usage, total deadlines,
retained reservations, and budget rejection before a retry. Engine fixtures
check that partial/extra actions never execute and rejected memory is not
promoted. Official benchmark grading remains an independent process.

Protocol references: [DeepSeek keep-alive behavior](https://api-docs.deepseek.com/quick_start/rate_limit/)
and [Chat Completions streaming schema](https://api-docs.deepseek.com/api/create-chat-completion/).
