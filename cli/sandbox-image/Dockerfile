# VaultShield Sandbox image: the runtime the agent and the target application
# both execute inside.
#
# `vaultshield run` defaults to `python:3.11`, which is enough for the Monitor's
# in-container watcher (`python`) and for `git clone`, but has neither Node/npm
# nor any agent CLI. This image is the reproducible answer for
# `--agent=antigravity` and for a Node target application:
#
#   git      `Sandbox._clone_repo` clones into /workspace/repo, and
#            `git_capture` commits + diffs the agent's work in place.
#   python   `sandbox_monitor._start_watcher` execs `python <watcher>`; a Run
#            whose watcher cannot start is failed deliberately, because an
#            unobserved Run looks like a clean one.
#   node/npm the target application's `start_command` (`npm run dev`) and
#            `Sandbox._install_dependencies`' `npm install`.
#   agy      the real Antigravity CLI, Linux build.
#
# Build (from the `cli/` directory):
#   docker build -t vaultshield-sandbox:local sandbox-image
#
# Then:
#   vaultshield run --agent=antigravity --base-image=vaultshield-sandbox:local ...
#
# ## No credentials live in this image
#
# Nothing here bakes in a token, a key, or a Google session, and no host path is
# copied in. That is deliberate and load-bearing:
#
#   * the repository-host token for a private clone is passed as the environment
#     of the single `git clone` exec (see sandbox.py's "private-repository
#     cloning" note) and is gone afterwards;
#   * the agent's own credential, if any, arrives through
#     `SandboxInvocation.env` for the agent exec only;
#   * application credentials arrive as the read-only `.env.sandbox` bind mount,
#     which holds dummy values only.
#
# So this image is safe to build, cache, and share. Rebuilding it grants nobody
# access to anything.

FROM node:20-bookworm-slim

# Pinned Antigravity CLI release. Both digests come from the vendor's own
# platform manifests at
# https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_<arch>.json
#
# Pinned rather than "latest" so an image built today and an image built next
# month run the same agent — a Run is evidence, and evidence whose tooling
# silently changed underneath it is worth less. The checksum is verified below
# and a mismatch fails the build, so a tampered or truncated download cannot
# become an image.
ARG AGY_VERSION=1.1.26
ARG AGY_BUILD=1.1.26-5550154686791680
ARG AGY_SHA512_ARM64=332dddb06ab4d901a44cfd4b9b358848230e64a64515a8e79b03822348adac9ce92d54cb4fc5119ef075edfba922820c926dfddf82d3a49f4ecdb6e6704dfc75
ARG AGY_SHA512_AMD64=80f2e7bf1fe0833487975b320b07176b82dd2cc2043b8acb4201b37b86d604af50718400b58af0f41adc68b389640f6ff95362da87a9ef1682b34258e83110b2

# Supplied by BuildKit. Declared so the RUN below can pick the matching build;
# the host that prompted this work is arm64, and CI is usually amd64.
ARG TARGETARCH

# git      clone + the Run's branch/diff capture
# python3  the Monitor's file-change watcher
# pip      the requirements.txt branch of `_install_dependencies`
# ca-certs HTTPS to github.com and to the agent's API
# curl     used by the agy download below, and kept out of the final layer
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-pip; \
    rm -rf /var/lib/apt/lists/*; \
    # `sandbox_monitor` execs `python`, not `python3`. Debian ships only
    # `python3`, so without this link the watcher never starts and every Run
    # fails with SandboxMonitorError.
    ln -sf /usr/bin/python3 /usr/local/bin/python

# The Antigravity CLI itself. Downloaded, checksum-verified, then installed as
# `agy` — the name `adapters/antigravity.py` invokes
# (`ANTIGRAVITY_EXECUTABLE = "agy"`). The vendor tarball contains a single
# binary named `antigravity`.
RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        arm64) agy_path='linux-arm/cli_linux_arm64.tar.gz'; agy_sha="${AGY_SHA512_ARM64}" ;; \
        amd64) agy_path='linux-x64/cli_linux_x64.tar.gz';   agy_sha="${AGY_SHA512_AMD64}" ;; \
        *) echo "unsupported architecture: ${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    url="https://storage.googleapis.com/antigravity-public/antigravity-cli/${AGY_BUILD}/${agy_path}"; \
    curl -fsSL -o /tmp/agy.tar.gz "$url"; \
    echo "${agy_sha}  /tmp/agy.tar.gz" | sha512sum -c -; \
    tar -xzf /tmp/agy.tar.gz -C /tmp; \
    install -m 0755 /tmp/antigravity /usr/local/bin/agy; \
    rm -f /tmp/agy.tar.gz /tmp/antigravity; \
    # Fails the build if the binary is the wrong architecture or is not
    # executable, which is the failure this image exists to prevent: the host's
    # own `agy` is a macOS Mach-O binary and cannot run here. `--help` is flag
    # parsing only, so it needs no network and no Google session. Bounded so a
    # binary that decides to block cannot hang the build.
    timeout 30 agy --help > /dev/null

# Matches `Sandbox.WORKDIR`. `create()` sets `working_dir` itself, so this is
# only the default for an interactive `docker run` against the image.
WORKDIR /workspace

# `create()` supplies `tail -f /dev/null` as the keep-alive command, so this
# CMD is only what a bare `docker run` gets. A shell is the useful default for
# inspecting the image by hand.
CMD ["bash"]
