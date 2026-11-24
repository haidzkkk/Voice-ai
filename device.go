package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"strings"
	"sync"
)

// Trộn thêm chuỗi riêng của app để device ID không liên kết được với app khác.
const deviceSalt = "giongai-device-v1"

var (
	deviceOnce sync.Once
	deviceID   string
)

// rawMachineID đọc mã máy ổn định của hệ điều hành (sống sót qua cài lại app).
func rawMachineID() string {
	switch runtime.GOOS {
	case "darwin":
		if out, err := exec.Command("ioreg", "-rd1", "-c", "IOPlatformExpertDevice").Output(); err == nil {
			if m := regexp.MustCompile(`"IOPlatformUUID"\s*=\s*"([^"]+)"`).FindSubmatch(out); m != nil {
				return string(m[1])
			}
		}
	case "windows":
		cmd := exec.Command("reg", "query", `HKLM\SOFTWARE\Microsoft\Cryptography`, "/v", "MachineGuid")
		hideWindow(cmd)
		if out, err := cmd.Output(); err == nil {
			f := strings.Fields(string(out))
			if len(f) > 0 {
				return f[len(f)-1]
			}
		}
	default: // linux
		for _, p := range []string{"/etc/machine-id", "/var/lib/dbus/machine-id"} {
			if b, err := os.ReadFile(p); err == nil {
				if s := strings.TrimSpace(string(b)); s != "" {
					return s
				}
			}
		}
	}
	h, _ := os.Hostname() // dự phòng khi không đọc được mã máy
	return h
}

// getDeviceID trả về mã thiết bị đã băm (hex), tính một lần rồi cache.
func getDeviceID() string {
	deviceOnce.Do(func() {
		raw := strings.TrimSpace(rawMachineID())
		if raw == "" {
			raw = "unknown"
		}
		sum := sha256.Sum256([]byte(deviceSalt + ":" + raw))
		deviceID = hex.EncodeToString(sum[:])
	})
	return deviceID
}

func registerDeviceAPI(mux *http.ServeMux) {
	mux.HandleFunc("/api/device", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]string{"id": getDeviceID()})
	})
}
