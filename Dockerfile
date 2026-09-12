FROM python:3.13-slim

# demoparser2 ships manylinux_2_17_aarch64 wheels for cp310-cp314, so the arm64
# image needs no Rust toolchain -- pip finds a wheel on both platforms.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY analyze.py team.py report.py cluster_run.py ./

RUN groupadd -r cs2 && useradd -r -g cs2 cs2 \
    && mkdir -p /scratch && chown cs2:cs2 /scratch
USER cs2

# Demos are downloaded, unpacked and deleted here, one at a time. Mounted as an
# emptyDir so nothing survives the pod and the node disk is never filled.
ENV SCRATCH=/scratch \
    R2_BUCKET="" \
    R2_ENDPOINT="" \
    FACEIT_NICKNAME="" \
    FACEIT_API_KEY=""

CMD ["python", "cluster_run.py"]
