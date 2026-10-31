# Stage 1: Build
FROM rust:1.70-slim AS builder
WORKDIR /usr/src/comfyctl
COPY . .
RUN cargo build --release

# Stage 2: Runtime
FROM alpine:3.18
WORKDIR /usr/local/bin
COPY --from=builder /usr/src/comfyctl/target/release/comfyctl .
EXPOSE 8080
CMD ["comfyctl"]