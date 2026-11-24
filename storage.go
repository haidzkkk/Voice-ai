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
	// Root là thư mục gốc duy nhất; mọi dữ liệu nằm trong các thư mục con của nó.
	Root string `json:"root"`
	// Các field cũ (bỏ dùng) — giữ để đọc cấu hình phiên bản trước không lỗi.
	Engine string `json:"engine,omitempty"`
	Audio  string `json:"audio,omitempty"`
	Voices string `json:"voices,omitempty"`
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
	return filepath.Join(dataRoot(), "voices")
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
			writeJSON(w, map[string]any{
				"saved": map[string]string{"root": saved.Root},
				"active": map[string]string{
					"root":     dataRoot(),
					"engine":   engineDir(),
					"audio":    outDir,
					"voices":   voiceStorageDir(),
					"previews": previewDir(),
				},
				"restart_required": saved.Root != activeStorage.Root,
				"root_env":         os.Getenv("GIONGAI_HOME") != "",
			})
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
		root, err := validateStoragePath(in.Root)
		if err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		path := storageConfigPath()
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		b, _ := json.MarshalIndent(storageSettings{Root: root}, "", "  ")
		if err := os.WriteFile(path, b, 0600); err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		writeJSON(w, map[string]any{"restart_required": root != activeStorage.Root})
	})
}
