//go:build windows

package main

import (
	"os/exec"
	"syscall"
)

// hideWindow không hiện cửa sổ đen khi chạy tiến trình con trên Windows.
func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
}
