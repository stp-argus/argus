# Build the manager binary
#
# `--platform=$BUILDPLATFORM` 이 핵심이다. 이게 없으면 buildx 가 arm64 타깃을 만들 때
# **빌더 스테이지 자체를 QEMU 로 에뮬레이션**해서 `go build` 가 흉내낸 arm64 위에서 돈다.
# 실측: 멀티아치 릴리스가 30분을 넘기고도 안 끝났다. Go 는 크로스컴파일이 기본기이므로
# 빌더는 네이티브(빌드 호스트 아키텍처)로 두고 GOARCH 만 넘기면 된다.
FROM --platform=$BUILDPLATFORM golang:1.26 AS builder
ARG TARGETOS
ARG TARGETARCH

WORKDIR /workspace
# Copy the Go Modules manifests
COPY go.mod go.mod
COPY go.sum go.sum
# cache deps before building and copying source so that we don't need to re-download as much
# and so that source changes don't invalidate our downloaded layer
RUN go mod download

# Copy the Go source (relies on .dockerignore to filter)
COPY . .

# Build
# the GOARCH has no default value to allow the binary to be built according to the host where the command
# was called. For example, if we call make docker-build in a local env which has the Apple Silicon M1 SO
# the docker BUILDPLATFORM arg will be linux/arm64 when for Apple x86 it will be linux/amd64. Therefore,
# by leaving it empty we can ensure that the container and binary shipped on it will have the same platform.
# 패키지 단위로 빌드한다. 파일을 나열하면 같은 패키지에 파일이 늘 때 조용히 빠진다.
#
# ⚠️ **이 이미지는 BuildKit 으로만 빌드된다.** `.dockerignore` 가 `**` 로 전부 무시한 뒤
# `!**/*.go` 로 되살리는 형태인데, classic builder 는 그 재포함을 다르게 처리해서
# 소스가 통째로 안 들어간다 (`stat /workspace/cmd: directory not found`).
# CI(GitHub Actions)는 BuildKit 이 기본이라 지금까지 드러나지 않았다.
# 로컬: `docker buildx build --load ...` 또는 `DOCKER_BUILDKIT=1`.
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH} go build -a -o manager ./cmd

# Use distroless as minimal base image to package the manager binary
# Refer to https://github.com/GoogleContainerTools/distroless for more details
FROM gcr.io/distroless/static:nonroot
WORKDIR /
COPY --from=builder /workspace/manager .
USER 65532:65532

ENTRYPOINT ["/manager"]
