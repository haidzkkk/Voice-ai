package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

const appID = "giongai-v1"
const appVersion = "1.4.0" // phiên bản engine; web so sánh để gợi ý cập nhật
const defaultPort = 17860

// UI chạy trên web (GitHub Pages); binary chỉ là engine chạy local.
const webUI = "https://haidzkkk.github.io/Voice-ai/"

var outDir string

// ---------------------- Công việc tạo giọng ----------------------

type job struct {
	ID      string `json:"id"`
	Status  string `json:"status"` // running | done | error | canceled
	Done    int    `json:"done"`
	Total   int    `json:"total"`
	Error   string `json:"error,omitempty"`
	File    string `json:"file,omitempty"`
	Preview bool   `json:"preview,omitempty"`
	cancel  context.CancelFunc
}

var (
	jobsMu sync.Mutex
	jobs   = map[string]*job{}
)

func snapshot(j *job) job {
	jobsMu.Lock()
	defer jobsMu.Unlock()
	return *j
}

var badName = regexp.MustCompile(`[<>:"/\\|?*\x00-\x1f]+`)
var emotionCuePattern = regexp.MustCompile(`(?i)\[(?:cười|cuoi|chuckle|thở dài|tho dai|sigh|hắng giọng|hang giong|clear throat)\]|<\|emotion_[123]\|>`)
var emotionStylePattern = regexp.MustCompile(`(?i)\[(?:vui|buon|gian|haohung|thitham|kechuyen|macdinh)\]`)

func fileNameFor(text string) string {
	words := strings.Fields(text)
	if len(words) > 6 {
		words = words[:6]
	}
	slug := badName.ReplaceAllString(strings.Join(words, "_"), "")
	slug = strings.Trim(slug, "._ ")
	if r := []rune(slug); len(r) > 40 {
		slug = string(r[:40])
	}
	if slug == "" {
		slug = "audio"
	}
	return time.Now().Format("20060102_150405") + "_" + slug + ".mp3"
}

type ttsRequest struct {
	Text    string `json:"text"`
	Voice   string `json:"voice"`
	Rate    int    `json:"rate"`
	Pitch   int    `json:"pitch"`
	Volume  int    `json:"volume"`
	Engine  string `json:"engine"`  // "" = Microsoft, "vieneu" | "voxcpm" = giọng AI trên máy
	Custom  bool   `json:"custom"`  // giọng cá nhân
	Preview bool   `json:"preview"` // nghe thử: lưu vào bộ nhớ đệm, không vào thư mục kết quả
	// VoxCPM: chế độ clone + audio đoạn trước (base64) và lời thoại của nó (cho nối tiếp)
	Mode       string `json:"mode"`
	PromptB64  string `json:"prompt_b64"`
	PromptText string `json:"prompt_text"`
	Desc       string `json:"desc"` // mô tả cách đọc (chỉ reference mode)
}

func previewDir() string { return filepath.Join(engineDir(), "previews") }

func previewName(in ttsRequest) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%v|%d|%d|%d|%s", in.Engine, in.Voice, in.Custom, in.Rate, in.Pitch, in.Volume, in.Text)
	if in.Engine == "voxcpm" {
		fmt.Fprintf(h, "|m:%s|d:%s|pt:%s|pb:%d", in.Mode, in.Desc, in.PromptText, len(in.PromptB64)) // cache phân biệt chế độ + mô tả + audio nối tiếp
	}
	if in.Custom && validVoiceID(in.Voice) && (in.Engine == "vieneu" || in.Engine == "voxcpm") {
		if metadata, err := os.ReadFile(filepath.Join(voiceStorageDir(), in.Voice+".json")); err == nil {
			h.Write(metadata)
			var profile struct {
				EmotionSamples map[string]string `json:"emotion_samples"`
			}
			if in.Engine == "voxcpm" && json.Unmarshal(metadata, &profile) == nil {
				keys := make([]string, 0, len(profile.EmotionSamples))
				for tag := range profile.EmotionSamples {
					keys = append(keys, tag)
				}
				sort.Strings(keys)
				for _, tag := range keys {
					id := profile.EmotionSamples[tag]
					if validVoiceID(id) {
						if sample, err := os.ReadFile(filepath.Join(voiceStorageDir(), id+".json")); err == nil {
							h.Write([]byte(tag))
							h.Write(sample)
						}
					}
				}
			}
		}
	}
	return hex.EncodeToString(h.Sum(nil)[:12])
}

func startJob(in ttsRequest) *job {
	text := in.Text
	o := TTSOptions{Voice: in.Voice, Rate: in.Rate, Pitch: in.Pitch, Volume: in.Volume}
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Hour)
	total := 1
	local := in.Engine == "vieneu" || in.Engine == "voxcpm"
	if !local {
		total = len(splitText(cleanText(text), maxChunkBytes))
	}
	j := &job{ID: uuidHex()[:12], Status: "running", Total: total, cancel: cancel, Preview: in.Preview}
	if in.Preview {
		for _, ext := range []string{".mp3", ".wav"} {
			if st, err := os.Stat(filepath.Join(previewDir(), previewName(in)+ext)); err == nil && st.Size() > 0 {
				cancel()
				j.Status, j.File, j.Done = "done", previewName(in)+ext, total
				jobsMu.Lock()
				jobs[j.ID] = j
				jobsMu.Unlock()
				return j
			}
		}
	}
	jobsMu.Lock()
	jobs[j.ID] = j
	jobsMu.Unlock()
	go func() {
		defer cancel()
		prog := func(d, t int) {
			jobsMu.Lock()
			j.Done, j.Total = d, t
			jobsMu.Unlock()
		}
		var mp3 []byte
		var err error
		ext := "mp3"
		if local {
			mp3, ext, err = synthEngine(ctx, in.Engine, j.ID, text, in.Voice, in.Custom, in.Rate, in.Volume, in.Mode, in.PromptB64, in.PromptText, in.Desc, prog)
		} else {
			mp3, err = Synthesize(ctx, text, o, prog)
		}
		jobsMu.Lock()
		defer jobsMu.Unlock()
		if err != nil {
			if ctx.Err() == context.Canceled {
				j.Status = "canceled"
			} else {
				j.Status, j.Error = "error", err.Error()
			}
			return
		}
		name := strings.TrimSuffix(fileNameFor(text), ".mp3") + "." + ext
		dir := outDir
		if in.Preview {
			dir, name = previewDir(), previewName(in)+"."+ext
			os.MkdirAll(dir, 0o755)
		}
		if werr := os.WriteFile(filepath.Join(dir, name), mp3, 0o644); werr != nil {
			j.Status, j.Error = "error", "không lưu được file: "+werr.Error()
			return
		}
		j.Status, j.File = "done", name
	}()
	return j
}

// ---------------------- HTTP ----------------------

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	json.NewEncoder(w).Encode(v)
}

var (
	voicesMu    sync.Mutex
	voicesCache []Voice
)

func handler(quit chan struct{}) http.Handler {
	mux := http.NewServeMux()

	// Chỉ còn API — UI do web (GitHub Pages) phục vụ, không nhúng trong binary nữa.
	mux.HandleFunc("/api/ping", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(appID)) })
	mux.HandleFunc("/api/version", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]string{"app": appID, "version": appVersion})
	})

	mux.HandleFunc("/api/voices", func(w http.ResponseWriter, r *http.Request) {
		voicesMu.Lock()
		cached := voicesCache
		voicesMu.Unlock()
		if cached != nil {
			writeJSON(w, map[string]any{"voices": cached, "online": true})
			return
		}
		vs, err := fetchVoices(r.Context())
		if err != nil {
			writeJSON(w, map[string]any{"voices": fallbackVoices, "online": false, "error": err.Error()})
			return
		}
		sort.SliceStable(vs, func(a, b int) bool { return vs[a].ShortName < vs[b].ShortName })
		voicesMu.Lock()
		voicesCache = vs
		voicesMu.Unlock()
		writeJSON(w, map[string]any{"voices": vs, "online": true})
	})

	mux.HandleFunc("/api/tts", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method", 405)
			return
		}
		var in ttsRequest
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 5<<20)).Decode(&in); err != nil {
			http.Error(w, "dữ liệu không hợp lệ", 400)
			return
		}
		if strings.TrimSpace(in.Text) == "" || in.Voice == "" {
			http.Error(w, "thiếu văn bản hoặc giọng", 400)
			return
		}
		if in.Engine != "vieneu" && emotionCuePattern.MatchString(in.Text) {
			http.Error(w, "Thẻ biểu cảm chỉ dùng với VieNeu. Chọn VieNeu hoặc bỏ thẻ khỏi nội dung.", 400)
			return
		}
		if emotionStylePattern.MatchString(in.Text) && (in.Engine != "voxcpm" || !in.Custom) {
			http.Error(w, "Thẻ cảm xúc cần chọn VoxCPM2 và một giọng cá nhân đã thiết lập mẫu cảm xúc.", 400)
			return
		}
		if (in.Engine == "vieneu" || in.Engine == "voxcpm") && currentEngine(in.Engine) == nil {
			http.Error(w, "giọng AI trên máy chưa sẵn sàng", 503)
			return
		}
		j := startJob(in)
		writeJSON(w, snapshot(j))
	})

	mux.HandleFunc("/api/job", func(w http.ResponseWriter, r *http.Request) {
		jobsMu.Lock()
		j := jobs[r.URL.Query().Get("id")]
		jobsMu.Unlock()
		if j == nil {
			http.NotFound(w, r)
			return
		}
		writeJSON(w, snapshot(j))
	})

	mux.HandleFunc("/api/cancel", func(w http.ResponseWriter, r *http.Request) {
		jobsMu.Lock()
		j := jobs[r.URL.Query().Get("id")]
		jobsMu.Unlock()
		if j != nil {
			j.cancel()
		}
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/history", func(w http.ResponseWriter, r *http.Request) {
		type item struct {
			Name string `json:"name"`
			Size int64  `json:"size"`
			Time int64  `json:"time"`
		}
		items := []item{}
		entries, _ := os.ReadDir(outDir)
		for _, e := range entries {
			if e.IsDir() || !isAudio(e.Name()) {
				continue
			}
			if info, err := e.Info(); err == nil {
				items = append(items, item{e.Name(), info.Size(), info.ModTime().Unix()})
			}
		}
		sort.Slice(items, func(a, b int) bool { return items[a].Time > items[b].Time })
		if len(items) > 30 {
			items = items[:30]
		}
		writeJSON(w, map[string]any{"items": items, "dir": outDir})
	})

	mux.HandleFunc("/files/", func(w http.ResponseWriter, r *http.Request) {
		name := filepath.Base(strings.TrimPrefix(r.URL.Path, "/files/"))
		if name == "." || name == "/" || !isAudio(name) {
			http.NotFound(w, r)
			return
		}
		if r.URL.Query().Get("dl") == "1" {
			w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename*=UTF-8''%s", urlPathEscape(name)))
		}
		http.ServeFile(w, r, filepath.Join(outDir, name))
	})

	mux.HandleFunc("/api/delete", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method", 405)
			return
		}
		name := filepath.Base(r.URL.Query().Get("name"))
		if isAudio(name) {
			os.Remove(filepath.Join(outDir, name))
		}
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/open-folder", func(w http.ResponseWriter, r *http.Request) {
		openPath(outDir)
		writeJSON(w, map[string]bool{"ok": true})
	})

	mux.HandleFunc("/api/quit", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method", 405)
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
		go func() { time.Sleep(300 * time.Millisecond); close(quit) }()
	})

	mux.HandleFunc("/preview/", func(w http.ResponseWriter, r *http.Request) {
		name := filepath.Base(strings.TrimPrefix(r.URL.Path, "/preview/"))
		if !isAudio(name) {
			http.NotFound(w, r)
			return
		}
		http.ServeFile(w, r, filepath.Join(previewDir(), name))
	})

	registerEngineAPI(mux)
	registerStorageAPI(mux)
	registerStorageMoveAPI(mux)
	registerDeviceAPI(mux)

	// Chỉ chấp nhận truy cập từ chính máy này qua localhost
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		host, _, _ := net.SplitHostPort(r.Host)
		if host != "127.0.0.1" && host != "localhost" {
			http.Error(w, "forbidden", 403)
			return
		}
		// UI web (GitHub Pages) gọi API engine cục bộ: cho phép CORS từ origin
		// hợp lệ + Private Network Access (Chrome đòi header này khi HTTPS gọi localhost).
		if origin := r.Header.Get("Origin"); allowedOrigin(origin) {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Add("Vary", "Origin")
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
			if r.Header.Get("Access-Control-Request-Private-Network") != "" {
				w.Header().Set("Access-Control-Allow-Private-Network", "true")
			}
		}
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if storageMoving() && strings.HasPrefix(r.URL.Path, "/api/") && r.URL.Path != "/api/settings/storage-move" && r.URL.Path != "/api/settings/storage" {
			http.Error(w, "Đang chuyển dữ liệu. Vui lòng chờ hoàn tất.", 503)
			return
		}
		if storageMoving() && r.URL.Path == "/api/settings/storage" && r.Method != "GET" {
			http.Error(w, "Đang chuyển dữ liệu", 409)
			return
		}
		mux.ServeHTTP(w, r)
	})
}

// allowedOrigin: origin được phép gọi API engine cục bộ — UI web trên GitHub Pages,
// và mọi origin localhost/127.0.0.1 (để dev và khi binary tự phục vụ UI).
func allowedOrigin(o string) bool {
	if o == "" {
		return false
	}
	if o == "https://haidzkkk.github.io" {
		return true
	}
	u, err := url.Parse(o)
	if err != nil {
		return false
	}
	h := u.Hostname()
	return h == "localhost" || h == "127.0.0.1"
}

func isAudio(n string) bool {
	n = strings.ToLower(n)
	return strings.HasSuffix(n, ".mp3") || strings.HasSuffix(n, ".wav")
}

func urlPathEscape(s string) string {
	var b strings.Builder
	for _, c := range []byte(s) {
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || strings.IndexByte("-_.~", c) >= 0 {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

func openPath(p string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		if strings.HasPrefix(p, "http") {
			cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", p)
		} else {
			cmd = exec.Command("explorer", p)
		}
	case "darwin":
		cmd = exec.Command("open", p)
	default:
		cmd = exec.Command("xdg-open", p)
	}
	cmd.Start()
}

func pickOutDir() string {
	if d := os.Getenv("GIONGAI_OUT"); d != "" {
		return d
	}
	if activeStorage.Audio != "" {
		return activeStorage.Audio
	}
	if home, err := os.UserHomeDir(); err == nil {
		docs := filepath.Join(home, "Documents")
		if st, err := os.Stat(docs); err == nil && st.IsDir() {
			return filepath.Join(docs, "GiongAI")
		}
		return filepath.Join(home, "GiongAI")
	}
	exe, _ := os.Executable()
	return filepath.Join(filepath.Dir(exe), "GiongAI_output")
}

func launchedViaScheme() bool {
	for _, a := range os.Args[1:] {
		if strings.HasPrefix(strings.ToLower(a), "giongai:") {
			return true
		}
	}
	return false
}

func main() {
	registerURLScheme() // đăng ký giao thức giongai:// (Windows) để web mở được engine
	viaScheme := launchedViaScheme()
	loadStorageSettings()
	outDir = pickOutDir()
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		fmt.Println("Khong tao duoc thu muc luu file:", err)
	}

	// Nếu tool đã đang chạy thì chỉ mở lại trình duyệt
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", defaultPort))
	if err != nil {
		c := http.Client{Timeout: 2 * time.Second}
		if resp, e := c.Get(fmt.Sprintf("http://127.0.0.1:%d/api/ping", defaultPort)); e == nil {
			b := make([]byte, 32)
			n, _ := resp.Body.Read(b)
			resp.Body.Close()
			if string(b[:n]) == appID {
				if !viaScheme {
					openPath(webUI)
				}
				return
			}
		}
		ln, err = net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			fmt.Println("Khong mo duoc cong mang:", err)
			waitExit()
			return
		}
	}
	addr := fmt.Sprintf("http://127.0.0.1:%d/", ln.Addr().(*net.TCPAddr).Port)

	quit := make(chan struct{})
	srv := &http.Server{Handler: handler(quit), ReadHeaderTimeout: 10 * time.Second}
	go srv.Serve(ln)

	fmt.Println("========================================================")
	fmt.Println("   GIONG AI ENGINE - DANG CHAY tren may ban   v" + appVersion)
	fmt.Println("   (day la 'server' tao giong; KHONG phai trang web)")
	fmt.Println("--------------------------------------------------------")
	fmt.Println("   >> MO TRINH DUYET va vao dia chi nay de dung:")
	fmt.Println("        " + webUI)
	fmt.Println("      (cua so trinh duyet se tu mo sau vai giay)")
	fmt.Println()
	fmt.Println("   >> GIU CUA SO NAY MO trong khi dang dung.")
	fmt.Println("   >> DONG cua so nay (hoac bam Ctrl+C) de TAT server.")
	fmt.Println("--------------------------------------------------------")
	fmt.Println("   File am thanh luu tai:")
	fmt.Println("        " + outDir)
	fmt.Println("   Dia chi noi bo:", addr)
	fmt.Println("========================================================")
	if os.Getenv("GIONGAI_NO_BROWSER") == "" && !viaScheme {
		openPath(webUI)
	}
	openLogFile()
	if readMarker() != nil {
		go startEngine("vieneu") // nạp sẵn mô hình giọng nâng cao
	}
	<-quit
	stopAllEngines()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	srv.Shutdown(ctx)
}

func waitExit() {
	if runtime.GOOS == "windows" {
		fmt.Println("Nhan Enter de thoat...")
		fmt.Scanln()
	}
}
