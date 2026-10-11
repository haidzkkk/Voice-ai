package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestEmotionCuesRequireVieNeu(t *testing.T) {
	for _, tc := range []struct {
		engine string
		status int
	}{
		{"", http.StatusBadRequest},
		{"f5", http.StatusBadRequest},
		{"vieneu", http.StatusServiceUnavailable}, // Valid cue reaches the model-readiness check.
	} {
		t.Run(tc.engine, func(t *testing.T) {
			body, err := json.Marshal(ttsRequest{Engine: tc.engine, Text: "Nghe hay quá [cười].", Voice: "test"})
			if err != nil {
				t.Fatal(err)
			}
			req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:17860/api/tts", strings.NewReader(string(body)))
			response := httptest.NewRecorder()
			handler(make(chan struct{})).ServeHTTP(response, req)
			if response.Code != tc.status {
				t.Fatalf("status = %d, want %d: %s", response.Code, tc.status, response.Body.String())
			}
		})
	}
}
