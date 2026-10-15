package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type storageSettings struct {
	Engine string `json:"engine"`
	Audio  string `json:"audio"`
	Voices string `json:"voices"`
}

// Active settings are loaded once: engines and in-flight jobs retain consistent paths.
var activeStorage storageSettings

func storageConfigPath() string {
	d, err := os.UserConfigDir()
	if err != nil {
		d = "."
	}
	return filepath.Join(d, "GiongAI", "storage.json")
}
func loadStorageSettings() {
	b, err := os.ReadFile(storageConfigPath())
	if err == nil {
		_ = json.Unmarshal(b, &activeStorage)
	}
}
func voiceStorageDir() string {
	if activeStorage.Voices != "" {
		return activeStorage.Voices
	}
	return filepath.Join(engineDir(), "voices")
}
func validateStoragePath(p string) (string, error) {
	p = strings.TrimSpace(p)
	if p == "" {
		return "", nil
	}
	if !filepath.IsAbs(p) {
		return "", fmt.Errorf("hãy nhập đường dẫn tuyệt đối: %s", p)
	}
	p = filepath.Clean(p)
	if err := os.MkdirAll(p, 0755); err != nil {
		return "", err
	}
	f, err := os.CreateTemp(p, ".giongai-write-check-*")
	if err != nil {
		return "", fmt.Errorf("không ghi được vào %s: %w", p, err)
	}
	name := f.Name()
	f.Close()
	os.Remove(name)
	return p, nil
}
func registerStorageAPI(mux *http.ServeMux) {
	mux.HandleFunc("/api/settings/choose-folder", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			w.WriteHeader(405)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Minute)
		defer cancel()
		var cmd *exec.Cmd
		switch runtime.GOOS {
		case "windows":
			cmd = exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-STA", "-Command", `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; $picker=New-Object System.Windows.Forms.FolderBrowserDialog; $picker.Description='GiongAI - Chon thu muc'; if($picker.ShowDialog() -eq 'OK'){[Console]::Write($picker.SelectedPath)}; $picker.Dispose()`)
		case "darwin":
			cmd = exec.CommandContext(ctx, "osascript", "-e", `POSIX path of (choose folder with prompt "Chọn thư mục lưu dữ liệu Giọng AI")`)
		default:
			cmd = exec.CommandContext(ctx, "zenity", "--file-selection", "--directory", "--title=Giọng AI: Chọn thư mục")
		}
		hideWindow(cmd)
		output, err := cmd.Output()
		if err != nil {
			http.Error(w, "Chưa chọn được thư mục. Bạn có thể nhập đường dẫn trực tiếp.", 400)
			return
		}
		writeJSON(w, map[string]string{"path": strings.TrimSpace(strings.TrimPrefix(string(output), "\ufeff"))})
	})
	mux.HandleFunc("/api/settings/storage", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			saved := activeStorage
			if b, err := os.ReadFile(storageConfigPath()); err == nil {
				_ = json.Unmarshal(b, &saved)
			}
			writeJSON(w, map[string]any{"saved": saved, "active": storageSettings{Engine: engineDir(), Audio: outDir, Voices: voiceStorageDir()}, "restart_required": saved != activeStorage, "engine_env": os.Getenv("GIONGAI_ENGINE_DIR") != "", "audio_env": os.Getenv("GIONGAI_OUT") != ""})
			return
		}
		if r.Method != "POST" {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		var in storageSettings
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16384)).Decode(&in); err != nil {
			http.Error(w, "Cài đặt không hợp lệ", 400)
			return
		}
		for _, p := range []*string{&in.Engine, &in.Audio, &in.Voices} {
			v, err := validateStoragePath(*p)
			if err != nil {
				http.Error(w, err.Error(), 400)
				return
			}
			*p = v
		}
		path := storageConfigPath()
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		b, _ := json.MarshalIndent(in, "", "  ")
		if err := os.WriteFile(path, b, 0600); err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		writeJSON(w, map[string]any{"restart_required": in != activeStorage})
	})
}
