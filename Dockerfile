FROM golang:1.24-bookworm AS build

WORKDIR /function

COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -mod=readonly -trimpath -ldflags="-s -w" -o /function/func .

FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /function/func /function/func

ENTRYPOINT ["/function/func"]
