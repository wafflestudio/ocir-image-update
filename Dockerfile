FROM golang:1.24-bookworm AS build

WORKDIR /function

COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -mod=readonly -trimpath -ldflags="-s -w" -o /function/func .

FROM scratch

COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build --chown=1000:1000 /function/func /function/func

USER 1000:1000
ENTRYPOINT ["/function/func"]
