import os

# One prediction scores one transfer, which gains nothing from parallel OpenMP threads. With the
# default (one thread per core), every concurrent request starts a team of threads for each
# prediction and they fight over the CPU; under load this capped payments throughput (see
# docs/performance.md). It must be set before scikit-learn loads OpenMP. An explicit
# OMP_NUM_THREADS still wins, for example to train faster.
os.environ.setdefault("OMP_NUM_THREADS", "1")
