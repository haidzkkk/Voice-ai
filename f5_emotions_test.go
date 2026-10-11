package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestF5EmotionSetupAndRemoval(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "voices")
	t.Setenv("GIONGAI_ENGINE_DIR", filepath.Dir(dir))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	write := func(id, metadata string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(dir, id+".json"), []byte(metadata), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, id+".wav"), []byte("test sample"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("abcdef12", `{"name":"Giọng gốc","speaker_emb":[1,2]}`)
	write("abcdef34", `{"name":"Mẫu vui","ref_text":"Lời thực tế","ref_text_verified":true}`)
	api := handler(make(chan struct{}))
	request := func(body string, expected int) {
		t.Helper()
		req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:17860/api/engine/emotions", strings.NewReader(body))
		response := httptest.NewRecorder()
		api.ServeHTTP(response, req)
		if response.Code != expected {
			t.Fatalf("status = %d, want %d: %s", response.Code, expected, response.Body.String())
		}
	}
	request(`{"id":"abcdef12","tag":"vui","sample_id":"abcdef34"}`, 200)
	data, err := os.ReadFile(filepath.Join(dir, "abcdef12.json"))
	if err != nil {
		t.Fatal(err)
	}
	var profile struct {
		EmotionSamples map[string]string `json:"emotion_samples"`
		SpeakerEmb     []float64         `json:"speaker_emb"`
	}
	if err := json.Unmarshal(data, &profile); err != nil || profile.EmotionSamples["vui"] != "abcdef34" || len(profile.SpeakerEmb) != 2 {
		t.Fatalf("mapping or voice data lost: %s", data)
	}
	in := ttsRequest{Engine: "f5", Custom: true, Voice: "abcdef12", Text: "[vui] Xin chào."}
	before := previewName(in)
	write("abcdef34", `{"ref_text":"Lời thoại đã sửa","ref_text_verified":true}`)
	if previewName(in) == before {
		t.Fatal("changed emotion transcript reused stale preview")
	}
	request(`{"id":"abcdef12","tag":"vui","sample_id":""}`, 200)
	if _, err := os.Stat(filepath.Join(dir, "abcdef34.wav")); err != nil {
		t.Fatal("unlinking an emotion must keep sample audio")
	}
	data, _ = os.ReadFile(filepath.Join(dir, "abcdef12.json"))
	if err := json.Unmarshal(data, &profile); err != nil {
		t.Fatal(err)
	}
	// Decode into a fresh map; encoding/json otherwise retains absent map keys.
	var removed map[string]any
	json.Unmarshal(data, &removed)
	if len(removed["emotion_samples"].(map[string]any)) != 0 {
		t.Fatal("emotion mapping was not removed")
	}
	write("abcdef34", `{"ref_text":"","ref_text_verified":false}`)
	request(`{"id":"abcdef12","tag":"vui","sample_id":"abcdef34"}`, 400)
	request(`{"id":"abcdef12","tag":"unknown","sample_id":"abcdef34"}`, 400)
	request(`{"id":"abcdef12","tag":"vui","sample_id":"abcdef99"}`, 404)
	request(`{"id":"../escape","tag":"vui","sample_id":"abcdef34"}`, 400)
}
