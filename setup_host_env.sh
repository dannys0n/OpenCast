#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
UPSTREAM_TAG="${UPSTREAM_TAG:-v0.18.0}"
UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/vllm-project/vllm-omni.git}"
UPSTREAM_DIR="${ROOT_DIR}/.cache/upstream/vllm-omni-${UPSTREAM_TAG}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
PYTHON_VERSION_REQUIRED="${PYTHON_VERSION_REQUIRED:-3.12}"
UV_BIN="${UV_BIN:-}"
RESOLVED_PYTHON_BIN=""

if [[ -z "${UV_BIN}" ]]; then
    for candidate in \
        "$(command -v uv 2>/dev/null || true)" \
        "${HOME}/.local/bin/uv" \
        "${HOME}/.cargo/bin/uv" \
        "/usr/local/bin/uv" \
        "/usr/bin/uv"; do
        if [[ -n "${candidate}" && -x "${candidate}" ]]; then
            UV_BIN="${candidate}"
            break
        fi
    done
fi

if [[ -z "${UV_BIN}" ]]; then
    echo "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/"
    echo "Current PATH: ${PATH}"
    echo "If uv is already installed under ~/.local/bin, rerun with:"
    echo "  PATH=\"\$HOME/.local/bin:\$PATH\" ./setup_host_env.sh"
    exit 1
fi

resolve_python_bin() {
    local candidate resolved version
    for candidate in \
        "${PYTHON_BIN}" \
        "$(command -v "${PYTHON_BIN}" 2>/dev/null || true)" \
        "${HOME}/.local/bin/python3.12" \
        "${HOME}/.pyenv/shims/python3.12" \
        "/usr/local/bin/python3.12" \
        "/usr/bin/python3.12" \
        "$(command -v python3 2>/dev/null || true)" \
        "$(command -v python 2>/dev/null || true)"; do
        if [[ -z "${candidate}" ]]; then
            continue
        fi
        if [[ -x "${candidate}" ]]; then
            resolved="${candidate}"
        else
            resolved="$(command -v "${candidate}" 2>/dev/null || true)"
        fi
        if [[ -z "${resolved}" || ! -x "${resolved}" ]]; then
            continue
        fi
        version="$("${resolved}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
        if [[ "${version}" == "${PYTHON_VERSION_REQUIRED}" ]]; then
            RESOLVED_PYTHON_BIN="${resolved}"
            return 0
        fi
    done
    return 1
}

if ! resolve_python_bin; then
    echo "Python ${PYTHON_VERSION_REQUIRED} is required."
    echo "Requested interpreter hint: ${PYTHON_BIN}"
    echo "Current PATH: ${PATH}"
    echo "If Python 3.12 is already installed under ~/.local/bin, rerun with:"
    echo "  PATH=\"\$HOME/.local/bin:\$PATH\" ./setup_host_env.sh"
    echo "Or point directly to it with:"
    echo "  PYTHON_BIN=\"\$HOME/.local/bin/python3.12\" ./setup_host_env.sh"
    exit 1
fi

mkdir -p "${ROOT_DIR}/.cache/upstream"

echo "Using uv at ${UV_BIN}"
echo "Using Python at ${RESOLVED_PYTHON_BIN}"
echo "Creating or reusing ${VENV_DIR}"
"${UV_BIN}" venv --allow-existing --python "${RESOLVED_PYTHON_BIN}" --seed "${VENV_DIR}"

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "Syncing local helper dependencies from pyproject.toml"
"${UV_BIN}" sync --python "${RESOLVED_PYTHON_BIN}"

echo "Installing vLLM 0.18.0"
"${UV_BIN}" pip install "vllm==0.18.0" --torch-backend=auto

# The prebuilt vLLM wheel still links against libcudart.so.12, while the
# torch stack pulled in by the default install path uses CUDA 13 packages.
# Installing the CUDA 12 runtime package keeps the host-native install working
# without requiring a local CUDA toolkit.
echo "Installing CUDA 12 runtime compatibility library"
"${UV_BIN}" pip install "nvidia-cuda-runtime-cu12==12.9.79"

if [[ ! -d "${UPSTREAM_DIR}/.git" ]]; then
    echo "Cloning vLLM-Omni ${UPSTREAM_TAG} into ${UPSTREAM_DIR}"
    git clone --branch "${UPSTREAM_TAG}" --depth 1 "${UPSTREAM_REPO}" "${UPSTREAM_DIR}"
else
    echo "Reusing existing upstream checkout at ${UPSTREAM_DIR}"
fi

echo "Installing vLLM-Omni from source"
"${UV_BIN}" pip install -e "${UPSTREAM_DIR}"

echo
echo "Environment is ready."
echo "Activate it with:"
echo "  source \"${VENV_DIR}/bin/activate\""
