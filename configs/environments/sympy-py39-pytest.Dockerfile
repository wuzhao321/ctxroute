# Build from an immutable, sanitized task image using a hash-locked wheelhouse.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY requirements.lock /tmp/ctxroute-tests/requirements.lock
COPY wheels/ /tmp/ctxroute-tests/wheels/
RUN /opt/miniconda3/envs/testbed/bin/python -m pip install \
        --no-index --no-deps --require-hashes \
        --find-links=/tmp/ctxroute-tests/wheels \
        -r /tmp/ctxroute-tests/requirements.lock \
    && /opt/miniconda3/envs/testbed/bin/python -m pip check \
    && /opt/miniconda3/envs/testbed/bin/python -m pytest --version \
    && rm -rf /tmp/ctxroute-tests
