package main

import (
	"strings"
	"testing"
)

func TestTorchBackendPinned(t *testing.T) {
	for _, tc := range []struct {
		gpu               bool
		os, suffix, index string
	}{
		{false, "windows", "+cpu", "/cpu"}, {true, "windows", "+cu128", "/cu128"}, {false, "darwin", "", ""},
	} {
		for _, pkg := range torchPackages(tc.gpu, tc.os) {
			if !strings.HasSuffix(pkg, "2.8.0"+tc.suffix) {
				t.Fatalf("incorrect pin: %s", pkg)
			}
		}
		if !strings.HasSuffix(torchIndex(tc.gpu, tc.os), tc.index) {
			t.Fatal("wrong index")
		}
	}
}
func TestMSVCRuntimeOnlyOnWindows(t *testing.T) {
	if pkgs := runtimePackages("windows"); len(pkgs) != 1 || !strings.HasPrefix(pkgs[0], "msvc-runtime==") {
		t.Fatalf("windows needs pinned msvc-runtime: %v", pkgs)
	}
	if pkgs := runtimePackages("darwin"); len(pkgs) != 0 {
		t.Fatalf("unexpected packages: %v", pkgs)
	}
}
func TestInstallerProgressTracksCompletedPackages(t *testing.T) {
	instMu.Lock()
	oldBusy, oldPackages, oldActivity, oldUpdated := installing, installPackages, installActivity, installUpdated
	installing = true
	installPackages = map[string]bool{}
	instMu.Unlock()
	defer func() {
		instMu.Lock()
		installing, installPackages, installActivity, installUpdated = oldBusy, oldPackages, oldActivity, oldUpdated
		instMu.Unlock()
	}()
	updateInstallActivity("Downloading torch (2.5GiB)")
	updateInstallActivity("Downloading torchaudio (2MiB)")
	updateInstallActivity(" Downloaded torchaudio")
	instMu.Lock()
	defer instMu.Unlock()
	if len(installPackages) != 2 || installPackages["torch"] || !installPackages["torchaudio"] {
		t.Fatalf("bad progress: %v", installPackages)
	}
	if installUpdated == 0 {
		t.Fatal("missing last activity time")
	}
}
