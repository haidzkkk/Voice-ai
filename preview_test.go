package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCustomPreviewChangesWhenReferenceTextIsCorrected(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("GIONGAI_ENGINE_DIR", dir)
	voices := filepath.Join(dir, "voices")
	if err := os.MkdirAll(voices, 0o755); err != nil {
		t.Fatal(err)
	}
	metadata := filepath.Join(voices, "abcdef12.json")
	write := func(body string) {
		t.Helper()
		if err := os.WriteFile(metadata, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	in := ttsRequest{Engine: "f5", Voice: "abcdef12", Custom: true, Text: "xin chào"}
	write(`{"ref_text":"văn bản mẫu sai"}`)
	before := previewName(in)
	if previewName(in) != before {
		t.Fatal("unchanged voice must retain its cached preview")
	}
	write(`{"ref_text":"lời thoại thực tế"}`)
	if previewName(in) == before {
		t.Fatal("corrected reference text must not reuse the old audio preview")
	}
}
