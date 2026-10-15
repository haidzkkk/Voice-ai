package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestStorageSettingsSaveAppliesOnRestart(t *testing.T) {
	root := t.TempDir()
	t.Setenv("HOME", root)
	t.Setenv("XDG_CONFIG_HOME", root)
	t.Setenv("APPDATA", root)
	old := activeStorage
	activeStorage = storageSettings{}
	defer func() { activeStorage = old }()
	in := storageSettings{Engine: filepath.Join(root, "engine"), Audio: filepath.Join(root, "audio"), Voices: filepath.Join(root, "voices")}
	body, _ := json.Marshal(in)
	mux := http.NewServeMux()
	registerStorageAPI(mux)
	resp := httptest.NewRecorder()
	mux.ServeHTTP(resp, httptest.NewRequest("POST", "/api/settings/storage", strings.NewReader(string(body))))
	if resp.Code != 200 {
		t.Fatal(resp.Body.String())
	}
	if activeStorage != (storageSettings{}) {
		t.Fatal("active paths changed before restart")
	}
	loadStorageSettings()
	if activeStorage != in {
		t.Fatalf("settings not restored: %+v", activeStorage)
	}
	if voiceStorageDir() != in.Voices {
		t.Fatal("voice directory ignored")
	}
}
func TestStorageRejectsRelativePaths(t *testing.T) {
	if _, err := validateStoragePath("relative/folder"); err == nil {
		t.Fatal("relative path accepted")
	}
	if value, err := validateStoragePath(" "); err != nil || value != "" {
		t.Fatal("empty default rejected")
	}
}
