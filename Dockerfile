# syntax=docker/dockerfile:1.7

ARG ALPINE_VERSION=3.24
ARG RUST_TOOLCHAIN=nightly
ARG RUST_TARGET=x86_64-unknown-linux-musl
ARG WINDOWS_GNU_TARGET=x86_64-pc-windows-gnu
ARG WASM_TARGET=wasm32-unknown-unknown

# ------------------------------------------------------------
# Stage 1: install Rust toolchain
# ------------------------------------------------------------
FROM alpine:${ALPINE_VERSION} AS rustup-nightly

ARG ALPINE_VERSION
ARG RUST_TOOLCHAIN
ARG RUST_TARGET
ARG WINDOWS_GNU_TARGET
ARG WASM_TARGET

ENV \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN set -eux; \
    printf '%s\n' \
      "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/main" \
      "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/community" \
      > /etc/apk/repositories; \
    apk add --no-cache \
      ca-certificates \
      curl \
      libgcc; \
    update-ca-certificates; \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
      sh -s -- \
        -y \
        --no-modify-path \
        --profile minimal \
        --default-toolchain none; \
    rustup toolchain install "${RUST_TOOLCHAIN}" \
      --profile minimal \
      --component rustfmt \
      --component clippy \
      --target "${RUST_TARGET}" \
      --target "${WINDOWS_GNU_TARGET}" \
      --target "${WASM_TARGET}" \
      --allow-downgrade; \
    rustup default "${RUST_TOOLCHAIN}"; \
    cargo --version; \
    rustc --version; \
    rustfmt --version; \
    cargo clippy --version; \
    rm -rf \
      /opt/rustup/downloads \
      /opt/rustup/tmp \
      /opt/cargo/registry \
      /opt/cargo/git \
      /root/.cargo \
      /root/.rustup \
      /root/.cache \
      /tmp/* \
      /var/tmp/*

# ------------------------------------------------------------
# Stage 2: final base image
# ------------------------------------------------------------
FROM alpine:${ALPINE_VERSION} AS base

ARG ALPINE_VERSION
ARG RUST_TARGET
ARG WINDOWS_GNU_TARGET

LABEL org.opencontainers.image.title="pylab.me/rust:base"
LABEL org.opencontainers.image.description="Rust nightly + Linux GCC/G++ musl toolchain + MinGW-w64 Windows GNU cross toolchain"
LABEL org.opencontainers.image.vendor="pylab.me"

ENV \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    RUST_BACKTRACE=1 \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    RUST_TARGET=${RUST_TARGET} \
    WINDOWS_GNU_TARGET=${WINDOWS_GNU_TARGET} \
    CARGO_TARGET_DIR=/work/target \
    CARGO_BUILD_TARGET=${RUST_TARGET} \
    CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    RUSTUP_TOOLCHAIN=nightly \
    CC=/usr/bin/cc \
    CXX=/usr/bin/c++ \
    AR=/usr/bin/ar \
    RANLIB=/usr/bin/ranlib \
    CC_x86_64_unknown_linux_musl=/usr/bin/cc \
    CXX_x86_64_unknown_linux_musl=/usr/bin/c++ \
    AR_x86_64_unknown_linux_musl=/usr/bin/ar \
    RANLIB_x86_64_unknown_linux_musl=/usr/bin/ranlib \
    CC_x86_64_pc_windows_gnu=x86_64-w64-mingw32-gcc \
    CXX_x86_64_pc_windows_gnu=x86_64-w64-mingw32-g++ \
    AR_x86_64_pc_windows_gnu=x86_64-w64-mingw32-ar \
    RANLIB_x86_64_pc_windows_gnu=x86_64-w64-mingw32-ranlib \
    PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN set -eux; \
    printf '%s\n' \
      "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/main" \
      "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/community" \
      > /etc/apk/repositories; \
    apk add --no-cache \
      bash \
      ca-certificates \
      file \
      gcc \
      g++ \
      git \
      libgcc \
      make \
      mingw-w64-gcc \
      musl-dev \
      openssh-client \
      pkgconf; \
    update-ca-certificates; \
    mkdir -p /work /out /opt/cargo /tmp/templates; \
    rm -rf \
      /root/.cache \
      /tmp/* \
      /var/tmp/*

COPY --from=rustup-nightly /opt/rustup /opt/rustup
COPY --from=rustup-nightly /opt/cargo /opt/cargo

# ------------------------------------------------------------
# Cargo config
# ------------------------------------------------------------
COPY <<'TPL' /tmp/templates/cargo-config.toml.tpl
[build]
target = "__RUST_TARGET__"
target-dir = "__CARGO_TARGET_DIR__"

[target.__RUST_TARGET__]
linker = "/usr/bin/cc"

[target.x86_64-pc-windows-gnu]
linker = "x86_64-w64-mingw32-gcc"

[profile.release]
lto = "thin"
codegen-units = 1
strip = "symbols"

[net]
git-fetch-with-cli = true
TPL

# ------------------------------------------------------------
# Helper scripts
# ------------------------------------------------------------
COPY <<'TPL' /tmp/templates/rust-check.tpl
__SHEBANG_BASH__
set -euo pipefail

features_args=()

if [[ "${ALL_FEATURES:-0}" == "1" ]]; then
  features_args+=("--all-features")
fi

if [[ "${NO_DEFAULT_FEATURES:-0}" == "1" ]]; then
  features_args+=("--no-default-features")
fi

if [[ -n "${FEATURES:-}" ]]; then
  features_args+=("--features" "${FEATURES}")
fi

echo "[rust-check] cargo fmt"
cargo fmt --all -- --check

echo "[rust-check] cargo clippy"
cargo clippy --workspace --all-targets "${features_args[@]}" -- -D warnings

echo "[rust-check] cargo check"
cargo check --workspace --all-targets "${features_args[@]}"
TPL

COPY <<'TPL' /tmp/templates/rust-pack.tpl
__SHEBANG_BASH__
set -euo pipefail

TARGET="${TARGET:-${CARGO_BUILD_TARGET:-__RUST_TARGET__}}"
PROFILE="${PROFILE:-release}"
BIN_NAME="${BIN_NAME:-}"
PACKAGE="${PACKAGE:-}"
FEATURES="${FEATURES:-}"

args=()

if [[ -n "${PACKAGE}" ]]; then
  args+=("-p" "${PACKAGE}")
fi

if [[ "${ALL_FEATURES:-0}" == "1" ]]; then
  args+=("--all-features")
fi

if [[ "${NO_DEFAULT_FEATURES:-0}" == "1" ]]; then
  args+=("--no-default-features")
fi

if [[ -n "${FEATURES}" ]]; then
  args+=("--features" "${FEATURES}")
fi

echo "[rust-pack] toolchain: ${RUSTUP_TOOLCHAIN:-default}"
echo "[rust-pack] target:    ${TARGET}"
echo "[rust-pack] profile:   ${PROFILE}"
echo "[rust-pack] out:       /out"

cargo build \
  --target "${TARGET}" \
  --profile "${PROFILE}" \
  "${args[@]}" \
  "$@"

mkdir -p /out

build_dir="${CARGO_TARGET_DIR:-__CARGO_TARGET_DIR__}/${TARGET}/${PROFILE}"

if [[ -n "${BIN_NAME}" ]]; then
  cp "${build_dir}/${BIN_NAME}" "/out/${BIN_NAME}"
else
  find "${build_dir}" \
    -maxdepth 1 \
    -type f \
    -perm -111 \
    -exec cp {} /out/ \;
fi

echo "[rust-pack] artifacts:"
ls -lh /out
TPL

# ------------------------------------------------------------
# Materialize templates
# ------------------------------------------------------------
RUN <<'SH'
set -eux

replace_placeholders() {
  file="$1"
  tmp="$(mktemp)"

  sed \
    -e 's|__SHEBANG_BASH__|#!/usr/bin/env bash|g' \
    -e "s|__RUST_TARGET__|${RUST_TARGET}|g" \
    -e "s|__CARGO_TARGET_DIR__|${CARGO_TARGET_DIR}|g" \
    "${file}" > "${tmp}"

  cat "${tmp}" > "${file}"
  rm -f "${tmp}"
}

install_template() {
  src="$1"
  dst="$2"
  mode="$3"

  cp "${src}" "${dst}"
  replace_placeholders "${dst}"
  chmod "${mode}" "${dst}"
}

install_template /tmp/templates/cargo-config.toml.tpl /opt/cargo/config.toml 0644
install_template /tmp/templates/rust-check.tpl /usr/local/bin/rust-check 0755
install_template /tmp/templates/rust-pack.tpl /usr/local/bin/rust-pack 0755

rm -rf /tmp/templates
SH

WORKDIR /work

# ------------------------------------------------------------
# Smoke test
# ------------------------------------------------------------
RUN set -eux; \
    cargo --version; \
    rustc --version; \
    rustfmt --version; \
    cargo clippy --version; \
    cc --version; \
    c++ --version; \
    x86_64-w64-mingw32-gcc --version; \
    x86_64-w64-mingw32-g++ --version; \
    cat /opt/cargo/config.toml; \
    mkdir -p /tmp/rust-smoke/src; \
    printf '%s\n' \
      '[package]' \
      'name = "rust-smoke"' \
      'version = "0.1.0"' \
      'edition = "2024"' \
      > /tmp/rust-smoke/Cargo.toml; \
    printf '%s\n' \
      'fn main() { println!("ok"); }' \
      > /tmp/rust-smoke/src/main.rs; \
    cargo build --release --manifest-path /tmp/rust-smoke/Cargo.toml --verbose; \
    file /work/target/x86_64-unknown-linux-musl/release/rust-smoke; \
    cargo build --release --target x86_64-pc-windows-gnu --manifest-path /tmp/rust-smoke/Cargo.toml --verbose; \
    file /work/target/x86_64-pc-windows-gnu/release/rust-smoke.exe; \
    rm -rf \
      /tmp/rust-smoke \
      /work/target \
      /opt/rustup/downloads \
      /opt/rustup/tmp \
      /opt/cargo/registry \
      /opt/cargo/git \
      /root/.cargo \
      /root/.rustup \
      /root/.cache \
      /tmp/* \
      /var/tmp/*

CMD ["bash"]
