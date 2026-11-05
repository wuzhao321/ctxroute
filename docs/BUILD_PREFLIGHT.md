# Build preflight evidence

Environment preparation is separate from inference and grading. Build scripts must
not receive model credentials, reference patches or hidden tests. Keep the official
installation commands and pinned repository contents intact; document any tool-version
constraint and the resulting image digests before inference.

`ctxroute.evalkit.harness_source` validates the source identity against the official
reset command. Harness `TestSpec` objects do not expose a `base_commit` attribute.
An inference image may remove the harness's empty setup commit only after checking
its parent, message and unchanged file tree. Other refs and unreachable history are
then removed. Unexpected tracked modifications stop preparation.

`ctxroute.evalkit.build_diagnostics.record_step` persists each subprocess result before
the caller checks its return code. Use a unique directory for every step. It records
separate standard output and error, exit status, timeout, operation/package context,
and Python, pip, setuptools and wheel versions before and after the operation. Run
version probes with the interpreter used by that operation; unavailable distributions
are recorded as null. Package names inferred from explicit errors are distinguished
from the package being installed. An unidentified nested failure is not guessed.

Output is not truncated. Redaction covers authorization headers, URL user information,
credential assignments, proxy endpoints and explicitly provided sensitive fixture
values. Incomplete lines are buffered so values split across pipe reads can be removed.
The helper does not discover or load credentials. Avoid secrets in build commands and
never publish raw or personal build records. If a lower-level builder combines streams,
preserve its complete redacted stream and identify that limitation; do not manufacture
separate upstream stdout/stderr.

When the helper runs in a disposable container, copy its evidence directory out in a
`finally` block **before** removing the container, including on nonzero exit or timeout.
Do not print results only after a sequence of assertions. An absent receipt is a failed
preflight, not an empty successful build. Keep the original failed attempt and use a
new evidence directory for each retry.
