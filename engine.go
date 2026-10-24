package main

// Cài đặt và điều khiển các engine giọng AI chạy trên máy (Python):
//   vieneu : VieNeu-TTS — 25 giọng có sẵn + phân tích giọng cá nhân (bắt buộc, cài trước)
//   f5     : F5-TTS tiếng Việt — rõ hơn, giống giọng mẫu hơn (tùy chọn)

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed engine/engine_server.py
var engineScript []byte

const engineVersion = "2"

const f5Package = "f5-tts @ https://github.com/nguyenthienhy/F5-TTS-Vietnamese/archive/e74db9d5a5e521bb930490e7cd7912438bf7ae84.zip"

// Câu mẫu để tạo giọng tham chiếu cho F5 từ giọng có sẵn của VieNeu (đã viết thường, không chữ số).
const presetRefText = "xin chào các bạn, hôm nay tôi sẽ kể cho các bạn nghe một câu chuyện thú vị về cuộc sống thường ngày."

type modelDef struct {
	ID     string
	Name   string
	Venv   string
	Marker string
	Python string
}

var modelDefs = map[string]*modelDef{
	"vieneu": {ID: "vieneu", Name: "VieNeu-TTS", Venv: "venv", Marker: "installed.json", Python: "3.12"},
	"f5":     {ID: "f5", Name: "F5-TTS tiếng Việt", Venv: "venv-f5", Marker: "installed-f5.json", Python: "3.11"},
}

func getModel(id string) (*modelDef, error) {
	if id == "" {
		id = "vieneu"
	}
	m := modelDefs[id]
	if m == nil {
		return nil, fmt.Errorf("mô hình không hợp lệ: %s", id)
	}
	return m, nil
}

// ---------------------------------------------------------------- đường dẫn

func engineDir() string {
	if d := os.Getenv("GIONGAI_ENGINE_DIR"); d != "" {
		return d
	}
	if activeStorage.Engine != "" {
		return activeStorage.Engine
	}
	switch runtime.GOOS {
	case "windows":
		if d := os.Getenv("LOCALAPPDATA"); d != "" {
			return filepath.Join(d, "GiongAI", "engine")
		}
	case "darwin":
		if h, err := os.UserHomeDir(); err == nil {
			return filepath.Join(h, "Library", "Application Support", "GiongAI", "engine")
		}
	}
	if h, err := os.UserHomeDir(); err == nil {
		return filepath.Join(h, ".local", "share", "GiongAI", "engine")
	}
	return "GiongAI-engine"
}

func exeName(n string) string {
	if runtime.GOOS == "windows" {
		return n + ".exe"
	}
	return n
}

func uvPath() string { return filepath.Join(engineDir(), "bin", exeName("uv")) }
func (m *modelDef) python() string {
	if runtime.GOOS == "windows" {
		return filepath.Join(engineDir(), m.Venv, "Scripts", "python.exe")
	}
	return filepath.Join(engineDir(), m.Venv, "bin", "python")
}
func (m *modelDef) markerPath() string { return filepath.Join(engineDir(), m.Marker) }
func scriptPath() string               { return filepath.Join(engineDir(), "engine_server.py") }

func childEnv() []string {
	d := engineDir()
	return append(os.Environ(),
		"UV_CACHE_DIR="+filepath.Join(d, "cache"),
		"UV_PYTHON_INSTALL_DIR="+filepath.Join(d, "python"),
		"UV_NO_CONFIG=1",
		"HF_HOME="+filepath.Join(d, "hf"),
		"HF_HUB_DISABLE_TELEMETRY=1",
		"HF_HUB_DISABLE_SYMLINKS_WARNING=1",
		// Mạng nghẽn khi tải file lớn sẽ báo lỗi sau 30s thay vì treo vô hạn;
		// download_model_file bắt lỗi này để thử lại và resume (xem engine_server.py).
		"HF_HUB_DOWNLOAD_TIMEOUT=30",
		"PYTHONUTF8=1",
		"PYTHONIOENCODING=utf-8",
		"PYTHONUNBUFFERED=1",
	)
}

type installInfo struct {
	Version string `json:"version"`
	GPU     bool   `json:"gpu"`
	Time    int64  `json:"time"`
}

func (m *modelDef) readMarker() *installInfo {
	b, err := os.ReadFile(m.markerPath())
	if err != nil {
		return nil
	}
	var mk installInfo
	if json.Unmarshal(b, &mk) != nil {
		return nil
	}
	if _, err := os.Stat(m.python()); err != nil {
		return nil
	}
	return &mk
}

func readMarker() *installInfo { return modelDefs["vieneu"].readMarker() }

// ------------------------------------------------------------ nhật ký (log)

type logBuf struct {
	mu    sync.Mutex
	lines []string
	file  *os.File
}

func (l *logBuf) add(s string) {
	s = strings.TrimRight(s, "\r\n")
	if s == "" {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if i := strings.LastIndex(s, "\r"); i >= 0 {
		s = s[i+1:]
	}
	l.lines = append(l.lines, s)
	if len(l.lines) > 300 {
		l.lines = l.lines[len(l.lines)-300:]
	}
	if l.file != nil {
		fmt.Fprintln(l.file, time.Now().Format("15:04:05"), s)
	}
}

func (l *logBuf) tail(n int) []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.lines) <= n {
		return append([]string{}, l.lines...)
	}
	return append([]string{}, l.lines[len(l.lines)-n:]...)
}

var engLog = &logBuf{}

func openLogFile() {
	os.MkdirAll(engineDir(), 0o755)
	f, err := os.OpenFile(filepath.Join(engineDir(), "engine.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err == nil {
		engLog.mu.Lock()
		engLog.file = f
		engLog.mu.Unlock()
	}
}

// ------------------------------------------------------------- phần cứng

type gpuInfo struct {
	Nvidia bool   `json:"nvidia"`
	Name   string `json:"name,omitempty"`
	VRAMMB int    `json:"vramMB,omitempty"`
}

var (
	gpuOnce   sync.Once
	gpuCached gpuInfo
)

func detectGPU() gpuInfo {
	gpuOnce.Do(func() {
		if runtime.GOOS != "windows" && runtime.GOOS != "linux" {
			return
		}
		cands := []string{"nvidia-smi"}
		if runtime.GOOS == "windows" {
			cands = append(cands, filepath.Join(os.Getenv("SystemRoot"), "System32", "nvidia-smi.exe"))
		}
		for _, c := range cands {
			cmd := exec.Command(c, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits")
			hideWindow(cmd)
			out, err := cmd.Output()
			if err != nil {
				continue
			}
			line := strings.TrimSpace(strings.Split(string(out), "\n")[0])
			if parts := strings.Split(line, ","); len(parts) >= 2 {
				gpuCached.Nvidia = true
				gpuCached.Name = strings.TrimSpace(parts[0])
				gpuCached.VRAMMB, _ = strconv.Atoi(strings.TrimSpace(parts[1]))
				return
			}
		}
	})
	return gpuCached
}

// ------------------------------------------------------------------ cài đặt

var (
	instMu          sync.Mutex
	installing      bool
	installModel    string
	installStep     string
	installErr      = map[string]string{}
	installPackages = map[string]bool{}
	installActivity string
	installUpdated  int64
)

func uvURL() (string, error) {
	if u := os.Getenv("GIONGAI_UV_URL"); u != "" {
		return u, nil
	}
	base := "https://github.com/astral-sh/uv/releases/latest/download/"
	switch runtime.GOOS + "/" + runtime.GOARCH {
	case "windows/amd64":
		return base + "uv-x86_64-pc-windows-msvc.zip", nil
	case "windows/arm64":
		return base + "uv-aarch64-pc-windows-msvc.zip", nil
	case "darwin/arm64":
		return base + "uv-aarch64-apple-darwin.tar.gz", nil
	case "darwin/amd64":
		return base + "uv-x86_64-apple-darwin.tar.gz", nil
	case "linux/amd64":
		return base + "uv-x86_64-unknown-linux-gnu.tar.gz", nil
	case "linux/arm64":
		return base + "uv-aarch64-unknown-linux-gnu.tar.gz", nil
	}
	return "", fmt.Errorf("chưa hỗ trợ hệ điều hành %s/%s", runtime.GOOS, runtime.GOARCH)
}

func downloadUV(ctx context.Context) error {
	if _, err := os.Stat(uvPath()); err == nil {
		return nil
	}
	u, err := uvURL()
	if err != nil {
		return err
	}
	engLog.add("Tải công cụ cài đặt: " + u)
	req, _ := http.NewRequestWithContext(ctx, "GET", u, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("tải uv thất bại (mã %d)", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 200<<20))
	if err != nil {
		return err
	}
	want := exeName("uv")
	var bin []byte
	if strings.HasSuffix(u, ".zip") {
		zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
		if err != nil {
			return err
		}
		for _, f := range zr.File {
			if filepath.Base(f.Name) == want {
				rc, err := f.Open()
				if err != nil {
					return err
				}
				bin, err = io.ReadAll(rc)
				rc.Close()
				if err != nil {
					return err
				}
			}
		}
	} else {
		gz, err := gzip.NewReader(bytes.NewReader(data))
		if err != nil {
			return err
		}
		tr := tar.NewReader(gz)
		for {
			h, err := tr.Next()
			if err == io.EOF {
				break
			}
			if err != nil {
				return err
			}
			if filepath.Base(h.Name) == want && h.Typeflag == tar.TypeReg {
				if bin, err = io.ReadAll(tr); err != nil {
					return err
				}
			}
		}
	}
	if bin == nil {
		return errors.New("không tìm thấy uv trong file tải về")
	}
	os.MkdirAll(filepath.Dir(uvPath()), 0o755)
	return os.WriteFile(uvPath(), bin, 0o755)
}

func runLogged(ctx context.Context, name string, args ...string) error {
	engLog.add("> " + filepath.Base(name) + " " + strings.Join(args, " "))
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = childEnv()
	cmd.Dir = engineDir()
	hideWindow(cmd)
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = pw, pw
	done := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 64*1024), 1<<20)
		sc.Split(scanLinesCR)
		for sc.Scan() {
			engLog.add(sc.Text())
			updateInstallActivity(sc.Text())
		}
		close(done)
	}()
	err := cmd.Run()
	pw.Close()
	<-done
	return err
}

// scanLinesCR tách dòng theo cả \n và \r (thanh tiến độ).
func scanLinesCR(data []byte, atEOF bool) (int, []byte, error) {
	if atEOF && len(data) == 0 {
		return 0, nil, nil
	}
	if i := bytes.IndexAny(data, "\r\n"); i >= 0 {
		return i + 1, data[:i], nil
	}
	if atEOF {
		return len(data), data, nil
	}
	return 0, nil, nil
}

func setStep(s string) {
	instMu.Lock()
	installStep = s
	installPackages = map[string]bool{}
	installActivity = s
	installUpdated = time.Now().Unix()
	instMu.Unlock()
	engLog.add("== " + s)
}

// Package progress comes from uv output; it is not a byte-download percentage.
var packageDownloadLine = regexp.MustCompile(`^\s*(Downloading|Downloaded)\s+([^\s(]+)`)

func updateInstallActivity(line string) {
	instMu.Lock()
	defer instMu.Unlock()
	if !installing {
		return
	}
	installActivity = line
	installUpdated = time.Now().Unix()
	if m := packageDownloadLine.FindStringSubmatch(line); len(m) > 0 {
		installPackages[m[2]] = m[1] == "Downloaded"
	}
}
func torchPackages(gpu bool, platform string) []string {
	suffix := ""
	if gpu {
		suffix = "+cu128"
	} else if platform == "windows" || platform == "linux" {
		suffix = "+cpu"
	}
	return []string{"torch==2.8.0" + suffix, "torchaudio==2.8.0" + suffix}
}
func torchIndex(gpu bool, platform string) string {
	if gpu {
		return "https://download.pytorch.org/whl/cu128"
	}
	if platform == "windows" || platform == "linux" {
		return "https://download.pytorch.org/whl/cpu"
	}
	return ""
}

// Thư viện C++ (onnxruntime, PyTorch) cần msvcp140.dll. Máy Windows mới hoặc máy ảo
// thường chưa cài VC++ Redistributable, nên chép DLL vào venv thay vì bắt người dùng cài.
// engine_server.py thêm thư mục venv vào đường tìm DLL.
func runtimePackages(platform string) []string {
	if platform == "windows" {
		return []string{"msvc-runtime==14.44.35112"}
	}
	return nil
}

func installTorch(ctx context.Context, py string, gpu bool) (bool, error) {
	setStep("Cài PyTorch CPU (không tải CUDA)")
	if gpu {
		setStep("Cài PyTorch NVIDIA CUDA — dung lượng lớn hơn bản CPU")
	}
	args := append([]string{"pip", "install", "--python", py}, torchPackages(gpu, runtime.GOOS)...)
	if index := torchIndex(gpu, runtime.GOOS); index != "" {
		args = append(args, "--index-url", index)
	}
	err := runLogged(ctx, uvPath(), args...)
	return gpu, err
}

func doInstall(m *modelDef, gpu bool) {
	defer func() {
		instMu.Lock()
		installing = false
		installStep = ""
		installModel = ""
		instMu.Unlock()
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Minute)
	defer cancel()
	fail := func(err error) {
		instMu.Lock()
		installErr[m.ID] = err.Error()
		instMu.Unlock()
		engLog.add("LỖI: " + err.Error())
	}
	stopEngine(m.ID)
	os.MkdirAll(engineDir(), 0o755)
	os.Remove(m.markerPath())
	uv := uvPath()
	py := m.python()

	setStep("Tải công cụ cài đặt")
	if err := downloadUV(ctx); err != nil {
		fail(fmt.Errorf("tải công cụ cài đặt: %w", err))
		return
	}
	setStep("Tải Python")
	if err := runLogged(ctx, uv, "python", "install", m.Python); err != nil {
		fail(fmt.Errorf("cài Python: %w", err))
		return
	}
	if err := runLogged(ctx, uv, "venv", "--python", m.Python, "--allow-existing", filepath.Join(engineDir(), m.Venv)); err != nil {
		fail(fmt.Errorf("tạo môi trường Python: %w", err))
		return
	}
	switch m.ID {
	case "vieneu":
		if gpu {
			var err error
			gpu, err = installTorch(ctx, py, true)
			if err == nil && gpu {
				err = runLogged(ctx, uv, "pip", "install", "--python", py, "transformers==4.57.6", "safetensors")
			}
			if err != nil || !gpu {
				runLogged(ctx, uv, "pip", "uninstall", "--python", py, "torch", "torchaudio")
				gpu = false
			}
		}
		setStep("Cài thư viện giọng VieNeu-TTS")
		args := append([]string{"pip", "install", "--python", py, "vieneu", "lameenc"}, runtimePackages(runtime.GOOS)...)
		if err := runLogged(ctx, uv, args...); err != nil {
			fail(fmt.Errorf("cài VieNeu-TTS: %w", err))
			return
		}
	case "f5":
		var err error
		if gpu, err = installTorch(ctx, py, gpu); err != nil {
			fail(fmt.Errorf("cài PyTorch: %w", err))
			return
		}
		setStep("Cài thư viện F5-TTS tiếng Việt")
		pkg := f5Package
		if v := os.Getenv("GIONGAI_F5_PACKAGE"); v != "" { // chỉ dùng để kiểm thử
			pkg = v
		}
		constraints := filepath.Join(engineDir(), "f5-torch-constraints.txt")
		if err := os.WriteFile(constraints, []byte(strings.Join(torchPackages(gpu, runtime.GOOS), "\n")+"\n"), 0600); err != nil {
			fail(err)
			return
		}
		args := []string{"pip", "install", "--python", py, "--constraint", constraints, pkg,
			"transformers>=4.45,<4.58", "pyarrow<20", "sea-g2p", "lameenc", "soundfile", "librosa"}
		args = append(args, runtimePackages(runtime.GOOS)...)
		if index := torchIndex(gpu, runtime.GOOS); index != "" {
			args = append(args, "--extra-index-url", index)
		}
		if err := runLogged(ctx, uv, args...); err != nil {
			fail(fmt.Errorf("cài F5-TTS: %w", err))
			return
		}
	}
	if err := os.WriteFile(scriptPath(), engineScript, 0o644); err != nil {
		fail(err)
		return
	}
	b, _ := json.Marshal(installInfo{Version: engineVersion, GPU: gpu, Time: time.Now().Unix()})
	os.WriteFile(m.markerPath(), b, 0o644)
	engLog.add("== Cài đặt xong " + m.Name)
	startEngine(m.ID)
}

func startInstall(id string, gpu bool) error {
	m, err := getModel(id)
	if err != nil {
		return err
	}
	if m.ID != "vieneu" && readMarker() == nil {
		return errors.New("hãy cài giọng AI nâng cao (VieNeu) trước")
	}
	instMu.Lock()
	defer instMu.Unlock()
	if installing {
		return errors.New("đang cài đặt, vui lòng chờ")
	}
	installing, installModel = true, m.ID
	delete(installErr, m.ID)
	go doInstall(m, gpu)
	return nil
}

// ------------------------------------------------------------- chạy engine

type engineProc struct {
	cmd   *exec.Cmd
	port  int
	token string
	stdin io.WriteCloser
	exit  chan struct{}
}

var (
	engMu  sync.Mutex
	engCur = map[string]*engineProc{}
	engErr = map[string]string{}
)

func freePort() int {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func startEngine(id string) error {
	m, err := getModel(id)
	if err != nil {
		return err
	}
	engMu.Lock()
	defer engMu.Unlock()
	if engCur[m.ID] != nil {
		return nil
	}
	if m.readMarker() == nil {
		return errors.New("chưa cài " + m.Name)
	}
	os.WriteFile(scriptPath(), engineScript, 0o644) // luôn dùng bản script đi kèm app
	p := &engineProc{port: freePort(), token: uuidHex(), exit: make(chan struct{})}
	args := []string{scriptPath(), "--port", fmt.Sprint(p.port), "--token", p.token, "--data", engineDir(), "--voices-dir", voiceStorageDir(), "--model", m.ID}
	if os.Getenv("GIONGAI_ENGINE_CPU") != "" {
		args = append(args, "--force-cpu")
	}
	cmd := exec.Command(m.python(), args...)
	cmd.Env = childEnv()
	cmd.Dir = engineDir()
	hideWindow(cmd)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	out, _ := cmd.StdoutPipe()
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		engErr[m.ID] = "không chạy được engine: " + err.Error()
		return err
	}
	engErr[m.ID] = ""
	p.cmd, p.stdin = cmd, stdin
	engCur[m.ID] = p
	engLog.add("== Khởi động " + m.Name)
	go func() {
		sc := bufio.NewScanner(out)
		sc.Buffer(make([]byte, 64*1024), 1<<20)
		sc.Split(scanLinesCR)
		for sc.Scan() {
			engLog.add(sc.Text())
		}
	}()
	go func() {
		err := cmd.Wait()
		close(p.exit)
		engMu.Lock()
		if engCur[m.ID] == p {
			delete(engCur, m.ID)
			if err != nil {
				engErr[m.ID] = m.Name + " đã dừng: " + err.Error() + " (xem nhật ký)"
			}
		}
		engMu.Unlock()
		engLog.add("== " + m.Name + " đã dừng")
	}()
	return nil
}

func stopEngine(id string) {
	engMu.Lock()
	p := engCur[id]
	delete(engCur, id)
	engMu.Unlock()
	if p == nil {
		return
	}
	p.stdin.Close() // engine tự thoát khi stdin đóng
	select {
	case <-p.exit:
	case <-time.After(3 * time.Second):
		p.cmd.Process.Kill()
	}
}

func stopAllEngines() {
	for id := range modelDefs {
		stopEngine(id)
	}
}

func currentEngine(id string) *engineProc {
	engMu.Lock()
	defer engMu.Unlock()
	return engCur[id]
}

var engClient = &http.Client{Transport: &http.Transport{Proxy: nil}}

func engCall(ctx context.Context, id, method, path string, body []byte) (*http.Response, error) {
	p := currentEngine(id)
	if p == nil {
		return nil, errors.New("engine chưa chạy")
	}
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, _ := http.NewRequestWithContext(ctx, method, fmt.Sprintf("http://127.0.0.1:%d%s", p.port, path), rd)
	req.Header.Set("X-Token", p.token)
	req.Header.Set("Content-Type", "application/json")
	return engClient.Do(req)
}

func readErr(resp *http.Response, b []byte) error {
	var e struct {
		Error string `json:"error"`
	}
	if json.Unmarshal(b, &e) == nil && e.Error != "" {
		return errors.New(e.Error)
	}
	return fmt.Errorf("engine trả về mã %d", resp.StatusCode)
}

func engJSON(ctx context.Context, id, method, path string, body []byte, out any) error {
	resp, err := engCall(ctx, id, method, path, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return readErr(resp, b)
	}
	if out != nil {
		return json.Unmarshal(b, out)
	}
	return nil
}

func engineReady(id string) bool {
	if currentEngine(id) == nil {
		return false
	}
	var h struct {
		State string `json:"state"`
	}
	c, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return engJSON(c, id, "GET", "/health", nil, &h) == nil && h.State == "ready"
}

// engAudio gửi yêu cầu tạo giọng, trả về dữ liệu âm thanh và đuôi file.
func engAudio(ctx context.Context, id string, body []byte) ([]byte, string, error) {
	resp, err := engCall(ctx, id, "POST", "/tts", body)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, "", err
	}
	if resp.StatusCode != 200 {
		return nil, "", readErr(resp, b)
	}
	ext := resp.Header.Get("X-Ext")
	if ext != "wav" {
		ext = "mp3"
	}
	return b, ext, nil
}

var refMu sync.Mutex

// presetRef tạo (một lần) file giọng mẫu cho F5 từ giọng có sẵn của VieNeu.
func presetRef(ctx context.Context, voice string) (string, error) {
	h := sha256.Sum256([]byte(voice + "|" + presetRefText))
	path := filepath.Join(engineDir(), "refs", hex.EncodeToString(h[:8])+".wav")
	refMu.Lock()
	defer refMu.Unlock()
	if st, err := os.Stat(path); err == nil && st.Size() > 1000 {
		return path, nil
	}
	if !engineReady("vieneu") {
		startEngine("vieneu")
		return "", errors.New("giọng VieNeu đang khởi động (cần để dùng giọng có sẵn) — thử lại sau ít giây")
	}
	body, _ := json.Marshal(map[string]any{"text": presetRefText, "voice": voice, "format": "wav"})
	data, ext, err := engAudio(ctx, "vieneu", body)
	if err != nil {
		return "", fmt.Errorf("tạo giọng mẫu: %w", err)
	}
	if ext != "wav" {
		return "", errors.New("giọng mẫu sai định dạng")
	}
	os.MkdirAll(filepath.Dir(path), 0o755)
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return "", err
	}
	return path, nil
}

// synthEngine tạo giọng bằng engine trên máy.
func synthEngine(ctx context.Context, model, jobID, text, voice string, custom bool, speed, volume int, progress func(d, t int)) ([]byte, string, error) {
	m, err := getModel(model)
	if err != nil {
		return nil, "", err
	}
	req := map[string]any{"job": jobID, "text": text, "speed": speed, "volume": volume}
	if m.ID == "vieneu" {
		req["voice"], req["custom"] = voice, custom
	} else if custom {
		req["custom_id"] = voice
	} else {
		ref, err := presetRef(ctx, voice)
		if err != nil {
			return nil, "", err
		}
		req["ref_wav"], req["ref_text"] = ref, presetRefText
	}
	body, _ := json.Marshal(req)
	type res struct {
		data []byte
		ext  string
		err  error
	}
	ch := make(chan res, 1)
	go func() {
		d, e, err := engAudio(ctx, m.ID, body)
		ch <- res{d, e, err}
	}()
	tk := time.NewTicker(700 * time.Millisecond)
	defer tk.Stop()
	for {
		select {
		case r := <-ch:
			return r.data, r.ext, r.err
		case <-ctx.Done():
			c, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			engCall(c, m.ID, "POST", "/cancel?job="+jobID, nil)
			cancel()
			<-ch
			return nil, "", ctx.Err()
		case <-tk.C:
			var pr struct{ Done, Total int }
			c, cancel := context.WithTimeout(ctx, 2*time.Second)
			if engJSON(c, m.ID, "GET", "/progress?job="+jobID, nil, &pr) == nil && pr.Total > 0 {
				progress(pr.Done, pr.Total)
			}
			cancel()
		}
	}
}

// ------------------------------------------------------------ HTTP cho UI

func modelStatus(m *modelDef, autostart bool) map[string]any {
	mk := m.readMarker()
	st := map[string]any{"installed": mk != nil}
	if mk != nil {
		st["gpuBuild"] = mk.GPU
	}
	instMu.Lock()
	st["installErr"] = installErr[m.ID]
	busy := installing && installModel == m.ID
	instMu.Unlock()
	st["installing"] = busy
	if busy {
		instMu.Lock()
		done := 0
		for _, complete := range installPackages {
			if complete {
				done++
			}
		}
		st["installProgress"] = map[string]any{"done": done, "total": len(installPackages), "activity": installActivity, "updated": installUpdated}
		instMu.Unlock()
	}
	if mk != nil && !busy && autostart && currentEngine(m.ID) == nil {
		engMu.Lock()
		prevErr := engErr[m.ID]
		engMu.Unlock()
		if prevErr == "" {
			startEngine(m.ID)
		}
	}
	engMu.Lock()
	st["running"] = engCur[m.ID] != nil
	st["engineErr"] = engErr[m.ID]
	engMu.Unlock()
	if currentEngine(m.ID) != nil {
		var h map[string]any
		c, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		if engJSON(c, m.ID, "GET", "/health", nil, &h) == nil {
			st["engine"] = h
		} else {
			st["engine"] = map[string]any{"state": "starting", "detail": "Đang khởi động…"}
		}
		cancel()
	}
	return st
}

func validVoiceID(id string) bool {
	if len(id) < 8 || len(id) > 32 {
		return false
	}
	for _, c := range id {
		if !strings.ContainsRune("0123456789abcdef", c) {
			return false
		}
	}
	return true
}

func postOnly(w http.ResponseWriter, r *http.Request) bool {
	if r.Method != http.MethodPost {
		http.Error(w, "method", 405)
		return false
	}
	return true
}

func registerEngineAPI(mux *http.ServeMux) {
	mux.HandleFunc("/api/engine/status", func(w http.ResponseWriter, r *http.Request) {
		auto := map[string]bool{}
		for _, a := range strings.Split(r.URL.Query().Get("autostart"), ",") {
			auto[a] = true
		}
		models := map[string]any{}
		for id, m := range modelDefs {
			models[id] = modelStatus(m, auto[id] || (id == "vieneu" && auto["1"]))
		}
		instMu.Lock()
		st := map[string]any{
			"models":          models,
			"installingModel": installModel,
			"step":            installStep,
			"os":              runtime.GOOS,
			"dir":             engineDir(),
		}
		instMu.Unlock()
		// các trường cũ (VieNeu) để giao diện dùng chung
		for k, v := range models["vieneu"].(map[string]any) {
			st[k] = v
		}
		if r.URL.Query().Get("probe") == "1" {
			st["gpu"] = detectGPU()
		}
		st["log"] = engLog.tail(14)
		writeJSON(w, st)
	})

	mux.HandleFunc("/api/engine/install", func(w http.ResponseWriter, r *http.Request) {
		if !postOnly(w, r) {
			return
		}
		var in struct {
			Model string `json:"model"`
			GPU   bool   `json:"gpu"`
		}
		json.NewDecoder(r.Body).Decode(&in)
		if err := startInstall(in.Model, in.GPU); err != nil {
			http.Error(w, err.Error(), 409)
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/engine/restart", func(w http.ResponseWriter, r *http.Request) {
		if !postOnly(w, r) {
			return
		}
		m, err := getModel(r.URL.Query().Get("model"))
		if err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		stopEngine(m.ID)
		engMu.Lock()
		engErr[m.ID] = ""
		engMu.Unlock()
		if err := startEngine(m.ID); err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/engine/uninstall", func(w http.ResponseWriter, r *http.Request) {
		if !postOnly(w, r) {
			return
		}
		m, err := getModel(r.URL.Query().Get("model"))
		if err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		instMu.Lock()
		busy := installing
		instMu.Unlock()
		if busy {
			http.Error(w, "đang cài đặt", 409)
			return
		}
		stopEngine(m.ID)
		os.Remove(m.markerPath())
		os.RemoveAll(filepath.Join(engineDir(), m.Venv)) // giọng cá nhân và mô hình đã tải được giữ lại
		engMu.Lock()
		engErr[m.ID] = ""
		engMu.Unlock()
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/engine/voices", func(w http.ResponseWriter, r *http.Request) {
		var out map[string]any
		if err := engJSON(r.Context(), "vieneu", "GET", "/voices", nil, &out); err != nil {
			http.Error(w, err.Error(), 503)
			return
		}
		writeJSON(w, out)
	})

	mux.HandleFunc("/api/engine/clone", func(w http.ResponseWriter, r *http.Request) {
		if !postOnly(w, r) {
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 30<<20))
		if err != nil {
			http.Error(w, "file quá lớn", 400)
			return
		}
		var out map[string]any
		if err := engJSON(r.Context(), "vieneu", "POST", "/clone", body, &out); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		writeJSON(w, out)
	})

	mux.HandleFunc("/api/engine/emotions", func(w http.ResponseWriter, r *http.Request) {
		if !postOnly(w, r) {
			return
		}
		var in struct {
			ID       string `json:"id"`
			Tag      string `json:"tag"`
			SampleID string `json:"sample_id"`
		}
		allowed := map[string]bool{"vui": true, "buon": true, "gian": true, "haohung": true, "thitham": true, "kechuyen": true}
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&in) != nil || !validVoiceID(in.ID) || !allowed[in.Tag] || (in.SampleID != "" && !validVoiceID(in.SampleID)) {
			http.Error(w, "dữ liệu cảm xúc không hợp lệ", 400)
			return
		}
		path := filepath.Join(voiceStorageDir(), in.ID+".json")
		data, err := os.ReadFile(path)
		var profile map[string]any
		if err != nil || json.Unmarshal(data, &profile) != nil {
			http.Error(w, "không tìm thấy giọng gốc", 404)
			return
		}
		if in.SampleID != "" {
			samplePath := filepath.Join(voiceStorageDir(), in.SampleID+".json")
			data, err := os.ReadFile(samplePath)
			var sample struct {
				RefText  string `json:"ref_text"`
				Verified bool   `json:"ref_text_verified"`
			}
			_, audioErr := os.Stat(filepath.Join(voiceStorageDir(), in.SampleID+".wav"))
			if err != nil || audioErr != nil || json.Unmarshal(data, &sample) != nil {
				http.Error(w, "không tìm thấy file mẫu cảm xúc", 404)
				return
			}
			if strings.TrimSpace(sample.RefText) == "" || !sample.Verified {
				http.Error(w, "Hãy nghe lại và xác nhận lời thoại mẫu trước khi lưu cảm xúc.", 400)
				return
			}
		}
		samples, _ := profile["emotion_samples"].(map[string]any)
		if samples == nil {
			samples = map[string]any{}
		}
		if in.SampleID == "" {
			delete(samples, in.Tag)
		} else {
			samples[in.Tag] = in.SampleID
		}
		profile["emotion_samples"] = samples
		data, err = json.Marshal(profile)
		if err == nil {
			var file *os.File
			file, err = os.CreateTemp(filepath.Dir(path), ".emotion-*.tmp")
			if err == nil {
				defer os.Remove(file.Name())
				_, err = file.Write(data)
				closeErr := file.Close()
				if err == nil {
					err = closeErr
				}
				if err == nil {
					err = os.Rename(file.Name(), path)
				}
			}
		}
		if err != nil {
			http.Error(w, "không lưu được cảm xúc: "+err.Error(), 500)
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/engine/transcribe", func(w http.ResponseWriter, r *http.Request) {
		if !postOnly(w, r) {
			return
		}
		if !engineReady("f5") {
			http.Error(w, "Hãy chọn F5-TTS và chờ mô hình sẵn sàng để tự nhận dạng lời thoại.", 503)
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 30<<20))
		if err != nil {
			http.Error(w, "file quá lớn", 400)
			return
		}
		var out map[string]any
		if err := engJSON(r.Context(), "f5", "POST", "/transcribe", body, &out); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		writeJSON(w, out)
	})

	mux.HandleFunc("/api/engine/voice-update", func(w http.ResponseWriter, r *http.Request) {
		if !postOnly(w, r) {
			return
		}
		var in struct {
			ID      string  `json:"id"`
			RefText *string `json:"ref_text"`
			Name    string  `json:"name"`
		}
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&in) != nil || !validVoiceID(in.ID) {
			http.Error(w, "dữ liệu không hợp lệ", 400)
			return
		}
		p := filepath.Join(voiceStorageDir(), in.ID+".json")
		b, err := os.ReadFile(p)
		if err != nil {
			http.Error(w, "không tìm thấy giọng", 404)
			return
		}
		var d map[string]any
		if json.Unmarshal(b, &d) != nil {
			http.Error(w, "file giọng hỏng", 500)
			return
		}
		if in.RefText != nil {
			t := strings.TrimSpace(*in.RefText)
			if r := []rune(t); len(r) > 500 {
				t = string(r[:500])
			}
			d["ref_text"] = t
			delete(d, "ref_text_auto")
			d["ref_text_verified"] = t != ""
		}
		if n := strings.TrimSpace(in.Name); n != "" {
			if r := []rune(n); len(r) > 60 {
				n = string(r[:60])
			}
			d["name"] = n
		}
		out, _ := json.Marshal(d)
		if err := os.WriteFile(p, out, 0o644); err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/engine/voice-delete", func(w http.ResponseWriter, r *http.Request) {
		if !postOnly(w, r) {
			return
		}
		id := r.URL.Query().Get("id")
		if !validVoiceID(id) {
			http.Error(w, "id", 400)
			return
		}
		os.Remove(filepath.Join(voiceStorageDir(), id+".json"))
		os.Remove(filepath.Join(voiceStorageDir(), id+".wav"))
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/engine/voice-sample", func(w http.ResponseWriter, r *http.Request) {
		id := r.URL.Query().Get("id")
		if !validVoiceID(id) {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "audio/wav")
		http.ServeFile(w, r, filepath.Join(voiceStorageDir(), id+".wav"))
	})

	mux.HandleFunc("/api/engine/open-log", func(w http.ResponseWriter, r *http.Request) {
		openPath(engineDir())
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/engine/open-models", func(w http.ResponseWriter, r *http.Request) {
		if err := os.MkdirAll(engineDir(), 0o755); err != nil {
			http.Error(w, "không tạo được thư mục mô hình: "+err.Error(), 500)
			return
		}
		openPath(engineDir())
		writeJSON(w, map[string]bool{"ok": true})
	})
}
