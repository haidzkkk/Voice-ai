//go:build windows

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
)

// hideWindow không hiện cửa sổ đen khi chạy tiến trình con trên Windows.
func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
}

// installedExe là vị trí cố định để cài engine: <root>/bin/GiongAI.exe
func installedExe() string {
	return filepath.Join(dataRoot(), "bin", "GiongAI.exe")
}

// registerURLScheme: tự cài engine vào thư mục cố định (để không hỏng khi user
// di chuyển/xoá file tải về) rồi đăng ký giao thức giongai:// trỏ vào bản cố định.
func registerURLScheme() {
	cur, err := os.Executable()
	if err != nil {
		return
	}
	target := installedExe()

	// Copy bản hiện tại vào thư mục cố định nếu đang chạy từ chỗ khác (vd Downloads).
	// Best-effort: nếu bản cài đang chạy (file bị khoá) thì bỏ qua, vẫn đăng ký bản cũ.
	if target != "" && !strings.EqualFold(filepath.Clean(cur), filepath.Clean(target)) {
		if data, e := os.ReadFile(cur); e == nil {
			if os.MkdirAll(filepath.Dir(target), 0o755) == nil {
				os.WriteFile(target, data, 0o755)
			}
		}
	}

	// Đăng ký trỏ vào bản cài cố định nếu nó tồn tại, ngược lại trỏ bản hiện tại.
	exe := target
	if exe == "" {
		exe = cur
	} else if _, e := os.Stat(exe); e != nil {
		exe = cur
	}

	const base = `HKCU\Software\Classes\giongai`
	run := func(args ...string) {
		cmd := exec.Command("reg", args...)
		hideWindow(cmd)
		cmd.Run()
	}
	run("add", base, "/ve", "/d", "URL:Giong AI", "/f")
	run("add", base, "/v", "URL Protocol", "/d", "", "/f")
	run("add", base+`\shell\open\command`, "/ve", "/d", `"`+exe+`" "%1"`, "/f")
}
