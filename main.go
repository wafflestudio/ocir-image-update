package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"time"

	fdk "github.com/fnproject/fdk-go"
)

func main() {
	log.SetFlags(0)
	fdk.Handle(fdk.HandlerFunc(handler))
}

func handler(ctx context.Context, in io.Reader, out io.Writer) {
	startedAt := time.Now()
	body, readErr := io.ReadAll(in)
	invocationFields := map[string]any{"body_bytes": len(body)}

	var event OCIEvent
	var result any
	statusCode := 200

	if readErr != nil {
		statusCode = 500
		result = errorResponse{Status: "error", Reason: fmt.Sprintf("Unable to read request body: %v", readErr)}
		emitFailureLog(startedAt, readErr, invocationFields)
	} else if err := json.Unmarshal(defaultJSONBody(body), &event); err != nil {
		statusCode = 500
		result = errorResponse{Status: "error", Reason: fmt.Sprintf("Invalid JSON event payload: %v", err)}
		emitFailureLog(startedAt, err, invocationFields)
	} else {
		invocationFields = merged(invocationFields, summarizeEventPayload(event))
		emitLog("invocation.started", invocationFields)

		processed, err := processEvent(ctx, event)
		if err != nil {
			if _, ok := err.(unsupportedEventError); ok {
				statusCode = 202
				result = errorResponse{Status: "ignored", Reason: err.Error()}
				emitLog("invocation.ignored", merged(invocationFields, fields{
					"duration_ms": time.Since(startedAt).Milliseconds(),
					"error_type":  fmt.Sprintf("%T", err),
					"reason":      err.Error(),
				}))
			} else {
				statusCode = 500
				result = errorResponse{Status: "error", Reason: err.Error()}
				emitFailureLog(startedAt, err, invocationFields)
			}
		} else {
			if processed.Status == "ignored" {
				statusCode = 202
			}
			result = processed
			emitLog("invocation.completed", merged(invocationFields, fields{
				"duration_ms":        time.Since(startedAt).Milliseconds(),
				"result_status":      processed.Status,
				"status_code":        statusCode,
				"updated_file_count": len(processed.UpdatedFiles),
			}))
		}
	}

	fdk.SetHeader(out, "Content-Type", "application/json")
	fdk.WriteStatus(out, statusCode)
	if err := json.NewEncoder(out).Encode(result); err != nil {
		log.Printf(`{"event":"response.encode_failed","reason":%q}`, err.Error())
	}
}

func defaultJSONBody(body []byte) []byte {
	if len(body) == 0 {
		return []byte("{}")
	}
	return body
}

func emitFailureLog(startedAt time.Time, err error, fields map[string]any) {
	emitLog("invocation.failed", merged(fields, map[string]any{
		"duration_ms": time.Since(startedAt).Milliseconds(),
		"error_type":  fmt.Sprintf("%T", err),
		"reason":      err.Error(),
	}))
}

type errorResponse struct {
	Status string `json:"status"`
	Reason string `json:"reason"`
}
