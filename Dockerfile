# syntax=docker/dockerfile:1.7

ARG ALPINE_VERSION=3.22
ARG RUST_TARGET=x86_64-unknown-linux-musl
ARG ZIG_CHANNEL=master
ARG ZIG_TARGET=x86_64-linux
ARG ZIG_CC_TARGET=x86_64-linux-musl

# ------------------------------------------------------------
# Stage 1: fetch Zig
# ------------------------------------------------------------
FROM alpine:${ALPINE_VERSION} AS zig-fetch

ARG ZIG_CHANNEL
ARG ZIG_TARGET

ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN set -eux; \
    apk add --no-cache ca-certificates curl jq tar xz; \
    update-ca-certificates; \
    ZIG_INDEX="$(mktemp)"; \
    curl -fsSL https://ziglang.org/download/index.json -o "${ZIG_INDEX}"; \
    ZIG_URL="$(jq -r --arg ch "${ZIG_CHANNEL}" --arg target "${ZIG_TARGET}" '.[$ch][$target].tarball' "${ZIG_INDEX}")"; \
    ZIG_SHA="$(jq -r --arg ch "${ZIG_CHANNEL}" --arg target "${ZIG_TARGET}" '.[$ch][$target].shasum' "${ZIG_INDEX}")"; \
    test -n "${ZIG_URL}"; \
    test "${ZIG_URL}" != "null"; \
    test -n "${ZIG_SHA}"; \
    test "${ZIG_SHA}" != "null"; \
    curl -fL "${ZIG_URL}" -o /tmp/zig.tar.xz; \
    echo "${ZIG_SHA}  /tmp/zig.tar.xz" | sha256sum -c -; \
    mkdir -p /opt/zig; \
    tar -xf /tmp/zig.tar.xz -C /opt/zig --strip-components=1; \
    /opt/zig/zig version; \
    rm -rf \
      /opt/zig/doc \
      /opt/zig/docs \
      /opt/zig/test \
      /opt/zig/samples \
      /tmp/zig.tar.xz \
      "${ZIG_INDEX}" \
      /tmp/*

# ------------------------------------------------------------
# Stage 2: install latest usable Rust nightly
# ------------------------------------------------------------
FROM alpine:${ALPINE_VERSION} AS rustup-nightly

ARG RUST_TARGET

ENV \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN set -eux; \
    apk add --no-cache ca-certificates curl libgcc; \
    update-ca-certificates; \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
      sh -s -- \
        -y \
        --no-modify-path \
        --profile minimal \
        --default-toolchain none; \
    rustup toolchain install nightly \
      --profile minimal \
      --component rustfmt \
      --component clippy \
      --target "${RUST_TARGET}" \
      --allow-downgrade; \
    rustup default nightly; \
    cargo +nightly --version; \
    rustc +nightly --version; \
    rustfmt +nightly --version; \
    cargo +nightly clippy --version; \
    rm -rf \
      /opt/rustup/downloads \
      /opt/rustup/tmp \
      /opt/cargo/registry \
      /opt/cargo/git \
      /root/.cargo \
      /root/.rustup \
      /root/.cache \
      /tmp/*

# ------------------------------------------------------------
# Stage 3: final image
# ------------------------------------------------------------
FROM alpine:${ALPINE_VERSION} AS base

ARG RUST_TARGET
ARG ZIG_CC_TARGET

LABEL org.opencontainers.image.title="pylab.me/rust:base"
LABEL org.opencontainers.image.description="Rust nightly + rustfmt + clippy + Zig musl base image"
LABEL org.opencontainers.image.vendor="pylab.me"

ENV \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    RUST_BACKTRACE=1 \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    RUST_TARGET=${RUST_TARGET} \
    ZIG_CC_TARGET=${ZIG_CC_TARGET} \
    CARGO_TARGET_DIR=/work/target \
    CARGO_BUILD_TARGET=${RUST_TARGET} \
    CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    RUSTUP_TOOLCHAIN=nightly \
    PKG_CONFIG_ALLOW_CROSS=1 \
    CC=/usr/local/bin/zigcc-musl \
    CXX=/usr/local/bin/zigcxx-musl \
    AR=/usr/local/bin/zig-ar \
    RANLIB=/usr/local/bin/zig-ranlib \
    CC_x86_64_unknown_linux_musl=/usr/local/bin/zigcc-musl \
    CXX_x86_64_unknown_linux_musl=/usr/local/bin/zigcxx-musl \
    AR_x86_64_unknown_linux_musl=/usr/local/bin/zig-ar \
    CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=/usr/local/bin/zigcc-musl \
    PATH=/opt/cargo/bin:/opt/zig:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN set -eux; \
    apk add --no-cache \
      bash \
      ca-certificates \
      git \
      openssh-client \
      make \
      pkgconf \
      musl-dev \
      libgcc; \
    update-ca-certificates; \
    rm -rf \
      /opt/rustup/downloads \
      /opt/rustup/tmp \
      /opt/cargo/registry \
      /opt/cargo/git \
      /root/.cargo \
      /root/.rustup \
      /root/.cache \
      /tmp/* \
      /var/tmp/*; \
    mkdir -p /work /out /opt/cargo /tmp/templates

COPY --from=zig-fetch /opt/zig /opt/zig
COPY --from=rustup-nightly /opt/rustup /opt/rustup
COPY --from=rustup-nightly /opt/cargo /opt/cargo

# ------------------------------------------------------------
# Templates: pure text, no Docker variable expansion
# ------------------------------------------------------------

COPY <<'TPL' /tmp/templates/zigcc-musl.tpl
__SHEBANG_SH__
exec /opt/zig/zig cc -target __ZIG_CC_TARGET__ "$@"
TPL

COPY <<'TPL' /tmp/templates/zigcxx-musl.tpl
__SHEBANG_SH__
exec /opt/zig/zig c++ -target __ZIG_CC_TARGET__ "$@"
TPL

COPY <<'TPL' /tmp/templates/zig-ar.tpl
__SHEBANG_SH__
exec /opt/zig/zig ar "$@"
TPL

COPY <<'TPL' /tmp/templates/zig-ranlib.tpl
__SHEBANG_SH__
exec /opt/zig/zig ranlib "$@"
TPL

COPY <<'TPL' /tmp/templates/cargo-config.toml.tpl
[build]
target = "__RUST_TARGET__"
target-dir = "__CARGO_TARGET_DIR__"

[target.__RUST_TARGET__]
linker = "/usr/local/bin/zigcc-musl"
ar = "/usr/local/bin/zig-ar"
rustflags = [
  "-C", "target-feature=+crt-static",
  "-C", "link-arg=-static"
]

[profile.release]
lto = "thin"
codegen-units = 1
strip = "symbols"

[net]
git-fetch-with-cli = true
TPL

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

echo "[rust-pack] toolchain: nightly"
echo "[rust-pack] target:    ${TARGET}"
echo "[rust-pack] profile:   ${PROFILE}"
echo "[rust-pack] linker:    /usr/local/bin/zigcc-musl"
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
# Materialize templates: one place for all replacements
# ------------------------------------------------------------
RUN <<'SH'
set -eux

replace_placeholders() {
  file="$1"

  tmp="$(mktemp)"

  sed \
    -e 's|__SHEBANG_SH__|#!/usr/bin/env sh|g' \
    -e 's|__SHEBANG_BASH__|#!/usr/bin/env bash|g' \
    -e "s|__RUST_TARGET__|${RUST_TARGET}|g" \
    -e "s|__ZIG_CC_TARGET__|${ZIG_CC_TARGET}|g" \
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

install_template /tmp/templates/zigcc-musl.tpl /usr/local/bin/zigcc-musl 0755
install_template /tmp/templates/zigcxx-musl.tpl /usr/local/bin/zigcxx-musl 0755
install_template /tmp/templates/zig-ar.tpl /usr/local/bin/zig-ar 0755
install_template /tmp/templates/zig-ranlib.tpl /usr/local/bin/zig-ranlib 0755
install_template /tmp/templates/rust-check.tpl /usr/local/bin/rust-check 0755
install_template /tmp/templates/rust-pack.tpl /usr/local/bin/rust-pack 0755
install_template /tmp/templates/cargo-config.toml.tpl /opt/cargo/config.toml 0644

rm -rf /tmp/templates
SH

WORKDIR /work

RUN set -eux; \
    cargo --version; \
    rustc --version; \
    rustfmt --version; \
    cargo clippy --version; \
    zig version; \
    zigcc-musl --version; \
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

CMD ["bash"]