# Disposable Docker data pilot

This runner collects real filesystem effects for a small H8 (request scope) pilot.
It does **not** execute arbitrary downloaded shell commands or reproduce the full
InterCode/RedCode benchmarks. Public data supplies task ideas, with source row,
revision, and file hashes retained. The coreutils operations and argument structure
are authored and reviewed locally. Cases without a matching source are marked
`authored-control`.

## Isolation boundary

Each case gets a new Docker Desktop Linux container. Its input arrives on stdin;
results return on stdout. There are no host directory mounts, Docker socket mounts,
shared data volumes, published ports, host namespaces, device passthrough, GPU access,
or external network. The root filesystem is read-only. Workspace and temporary
storage are private tmpfs filesystems, bounded to 32 MiB and 16 MiB respectively.
The process runs as UID 1000, with all capabilities dropped, no-new-privileges,
Docker's default seccomp filter, 256 MiB RAM, no swap, one CPU, and 32 processes.
Commands have a 3-second deadline and the container attachment has a 25-second
deadline. The created container ID is removed in `finally`, including after failures.

Before any case starts, the host checks the actual `docker inspect` configuration.
A separate preflight container verifies privileges, network namespace and blocked
egress, read-only root, absence of host paths and Docker socket, cgroup limits,
bounded storage, actual ENOSPC, and child timeout cleanup. These are concrete
checks of this configuration, not a claim that containers prevent all kernel exploits.

The worker accepts only copy, move, delete, truncate, touch, mkdir, and chmod-755
operations with literal validated relative paths. It rejects shell text and raw
commands. Setup supports regular files and directories only. Paths cannot escape
the workspace, introduce options, or contain shell syntax. The image contains only
the reviewed worker plus an official Python base pinned by digest. Build context
is restricted to the Dockerfile and worker; no checkout or credentials are copied.

## Labels and splits

For each repository fixture, two requests are crossed with two command targets:
the requested target and the other target. Each request and each effect occurs
with both labels. This prevents either input alone from solving the task. File
hashes, sizes, kinds and modes are measured before and after execution. Labels
compare that delta against a separate task-scope specification. Failed commands,
timeouts, unsupported entries, and unexpected effects cannot receive binary labels.

The initial pilot has 80 training, 40 calibration, and 40 evaluation cases. Each
fold has disjoint fixtures and request templates. Move and mkdir are held out from
training and calibration for the development evaluation. The audit requires zero
exact prompt overlap between folds, disjoint repository identities, and 50% maximum
lookup accuracy using only requests or only effects. It does not access dev-gen or
final-test. Only 20 harmful and 20 benign evaluation cases are available, so this
is an engineering pilot, not evidence sufficient for promotion or release claims.

The legacy binary label `destructive` means a policy violation here: all positives
are H8 scope mismatches, including unwanted creation or mode changes. It does not
imply that every positive irreversibly destroyed data. Labels are automatic and
have not been independently reviewed by a human. A training pass may validate the
pipeline; promotion still requires human label review and the research gates.

Effects use the existing EffectRecord shape for the model, with a collection
manifest specifying their restricted coverage. There is no syscall tracing,
git-history fixture, credential access, or resource-abuse corpus. Empty network,
process and git fields must not be interpreted as evidence of full monitoring.
The fixed reviewed operations and isolated fixtures exclude those behaviors by
construction. Expanding to arbitrary adversarial code requires a separate reviewed
collector and stronger observation coverage.

## Reproduction

Use the repository Python environment with `PYTHONPATH=src`. The Docker orchestration
works from Windows Python; training uses the existing WSL CUDA environment. The
Docker context is explicitly `desktop-linux`. `DRYRUN_DOCKER` may name the CLI binary.

```text
python -m research.container_pilot fetch research/generated/external/intercode
python -m research.container_pilot build python@sha256:<recorded-base-digest> research/generated/container-image.json
python -m research.container_pilot preflight sha256:<built-image-id> research/generated/container-preflight.json
python -m research.container_pilot collect sha256:<built-image-id> research/generated/external/intercode research/generated/container-pilot-v1
python -m research.container_pilot label research/generated/container-pilot-v1 research/generated/container-pilot-labels-v1
```

Raw measurements and source manifests remain immutable. Relabelling reads and
verifies those artifacts, writes a new directory, and never reruns a command.
Training and evaluation use only the exported item files. Preserve every manifest,
training/config hash, baseline result, and labeler hash with the experiment.

## Docker startup repair on this machine

Docker Desktop initially failed on stale zero-byte AF_UNIX reparse points in its
`run` directory and the `docker-secrets-engine` runtime directory. With Docker
stopped, those directories were renamed to dated `.stale-20260927-*` backups so
Docker could recreate the sockets. No images, volumes, settings, or stored secrets
were deleted. Engine 29.5.3 started successfully after both were repaired together.
